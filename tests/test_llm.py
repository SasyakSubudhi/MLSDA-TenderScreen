"""Part 4 - LLM gateway, circuit breaker, retries, stub, cost accounting."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from tenderscreen.llm import stub_logic
from tenderscreen.llm.breaker import CircuitBreaker
from tenderscreen.llm.client import price


def test_breaker_opens_after_threshold_and_half_opens():
    b = CircuitBreaker(failure_threshold=3, open_seconds=0.2)
    for _ in range(3):
        assert b.allow()
        b.record_failure()
    assert b.state == "open" and not b.allow()
    time.sleep(0.25)
    assert b.allow() and b.state == "half_open"
    assert not b.allow()          # only one probe at a time
    b.record_success()
    assert b.state == "closed" and b.allow()


def test_breaker_reopens_when_probe_fails():
    b = CircuitBreaker(failure_threshold=1, open_seconds=0.05)
    b.record_failure()
    time.sleep(0.06)
    assert b.allow()
    b.record_failure()
    assert b.state == "open" and b.opens_total == 2


def test_price_uses_injected_list_prices():
    assert price(1_000_000, 1_000_000) == pytest.approx(0.50)


def test_stub_answers_each_task_in_the_shape_asked():
    plan = json.loads(stub_logic.respond([{"role": "system", "content": "TASK: plan"},
                                          {"role": "user", "content": '<<INPUT>>{"candidates":[{"id":"A1"},{"id":"A2"}],'
                                                                      '"max_actions":1}<<END>>'}]))
    assert plan["actions"] == ["A1"]
    q = json.loads(stub_logic.respond([{"role": "system", "content": "TASK: derive_queries\nQUERY_SCOPE: tender_and_central_rules"},
                                       {"role": "user", "content": '<<INPUT>>{"conditions":[{"condition_id":"C1_TURNOVER",'
                                                                   '"bidder_category":"startup"}]}<<END>>'}]))
    assert q["queries"]["C1_TURNOVER"][0]["family"] == "policy" and "Startups" in q["queries"]["C1_TURNOVER"][0]["query"]


@pytest.fixture()
def gateway(fake_redis, monkeypatch):
    import tenderscreen.llm.gateway as gw

    monkeypatch.setattr(gw, "BASE_URL", "http://127.0.0.1:9/v1")   # nothing listens on port 9
    monkeypatch.setattr(gw, "MAX_ATTEMPTS", 2)
    monkeypatch.setattr(gw, "BACKOFF_BASE_S", 0.01)
    monkeypatch.setattr(gw, "PROVIDER_TIMEOUT_S", 1.0)
    monkeypatch.setattr(gw, "CACHE_ENABLED", False)
    monkeypatch.setattr(gw, "breaker", CircuitBreaker(4, 20))
    monkeypatch.setattr(gw, "get_redis", lambda: fake_redis)
    return TestClient(gw.app), gw


def _body():
    return {"task": "plan", "messages": [{"role": "user", "content": "hi"}], "run_id": "r", "agent": "research"}


def test_gateway_retries_then_opens_breaker_and_fails_fast(gateway):
    client, gw = gateway
    r1 = client.post("/v1/complete", json=_body())
    assert r1.status_code == 503 and r1.json()["detail"]["error"] == "provider_unavailable"
    assert gw.breaker.failures == 2                       # 2 attempts = 1 retry with backoff
    client.post("/v1/complete", json=_body())
    assert gw.breaker.state == "open"
    t0 = time.time()
    r3 = client.post("/v1/complete", json=_body())
    assert r3.json()["detail"]["error"] == "circuit_open" and time.time() - t0 < 0.5
    assert client.get("/health").json()["breaker"]["state"] == "open"
    client.post("/admin/breaker/reset")
    assert gw.breaker.state == "closed"


def test_gateway_budget_cap(gateway, fake_redis):
    client, gw = gateway
    fake_redis.set(gw._budget_key(), 999)
    r = client.post("/v1/complete", json=_body())
    assert r.status_code == 503 and r.json()["detail"]["error"] == "daily_budget_exhausted"


def test_resource_exporter_reports_every_container():
    import httpx

    from tenderscreen.llm import resource_exporter as rx

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/containers/json":
            return httpx.Response(200, json=[
                {"Id": "a1", "Names": ["/p-redis-1"], "State": "running", "Labels": {"com.docker.compose.service": "redis"}},
                {"Id": "b2", "Names": ["/p-grafana-1"], "State": "exited", "Labels": {"com.docker.compose.service": "grafana"}}])
        if path.endswith("/json"):
            return httpx.Response(200, json={"RestartCount": 1, "State": {"StartedAt": "2026-01-01T00:00:00.000Z"}})
        return httpx.Response(200, json={"cpu_stats": {"cpu_usage": {"total_usage": 2_000_000_000}},
                                         "memory_stats": {"usage": 50_000_000, "limit": 1_000_000_000,
                                                          "stats": {"inactive_file": 10_000_000}}})

    c = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://docker")
    rx.scrape(c, "p")
    assert rx.MEM.labels("redis", "p-redis-1")._value.get() == 40_000_000
    assert rx.UP.labels("grafana", "p-grafana-1")._value.get() == 0
    assert rx.UP.labels("redis", "p-redis-1")._value.get() == 1
    assert rx.UPTIME.labels("redis", "p-redis-1")._value.get() > 0
    assert rx.RESTARTS.labels("grafana", "p-grafana-1")._value.get() == 1
