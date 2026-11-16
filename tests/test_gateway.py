"""Part 4 - LLM gateway: file-backed provider key and sanitised provider errors."""
import json
import logging

import httpx
import pytest

from tenderscreen.llm import gateway

CANARY = "sk-synthetic-canary-0000"      # synthetic marker, not a credential


# ---------------------------------------------------------------- provider key
def test_key_file_wins_over_environment_variable(tmp_path):
    f = tmp_path / "llm_api_key"
    f.write_text(CANARY + "\n")
    assert gateway.resolve_api_key({"LLM_API_KEY_FILE": str(f), "LLM_API_KEY": "from-env"}) == CANARY


def test_environment_variable_still_works_without_a_file():
    assert gateway.resolve_api_key({"LLM_API_KEY": "from-env"}) == "from-env"


def test_default_is_keyless():
    assert gateway.resolve_api_key({}) == ""
    assert gateway.resolve_api_key({"LLM_API_KEY_FILE": ""}) == ""


def test_unreadable_key_file_fails_without_leaking(tmp_path):
    missing = tmp_path / "no_such_secret"
    with pytest.raises(gateway.ApiKeyConfigError) as err:
        gateway.resolve_api_key({"LLM_API_KEY_FILE": str(missing), "LLM_API_KEY": CANARY})
    assert CANARY not in str(err.value) and "cannot be read" in str(err.value)


def test_empty_key_file_fails_without_falling_back_to_env(tmp_path):
    f = tmp_path / "llm_api_key"
    f.write_text("  \n")
    with pytest.raises(gateway.ApiKeyConfigError) as err:
        gateway.resolve_api_key({"LLM_API_KEY_FILE": str(f), "LLM_API_KEY": CANARY})
    assert CANARY not in str(err.value) and "empty" in str(err.value)


# ------------------------------------------------------- provider error bodies
def test_rejected_request_never_exposes_the_provider_body(fake_redis, monkeypatch, caplog):
    from fastapi.testclient import TestClient

    body = {"error": {"message": f"Incorrect API key provided: {CANARY}", "type": "invalid_request_error"}}

    async def post(url, **kw):
        return httpx.Response(401, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(gateway._http, "post", post)
    monkeypatch.setattr(gateway, "CACHE_ENABLED", False)
    gateway.breaker.reset()
    before = gateway.breaker.snapshot()

    caplog.set_level(logging.DEBUG)
    client = TestClient(gateway.app)
    resp = client.post("/v1/complete", json={"task": "plan", "messages": [{"role": "user", "content": "hi"}],
                                             "run_id": "r-401", "agent": "research"})

    assert resp.status_code == 502
    assert resp.json() == {"detail": {"error": "provider_rejected_request", "status": 401}}
    assert CANARY not in resp.text
    assert CANARY not in caplog.text
    assert CANARY not in client.get("/metrics").text
    assert CANARY not in json.dumps(client.get("/health").json())
    # the provider is healthy, our request was wrong: the breaker is untouched
    assert gateway.breaker.snapshot()["state"] == before["state"] == "closed"


def test_agent_side_error_carries_only_the_code_and_status(monkeypatch):
    from tenderscreen.llm.client import GatewayClient, LLMUnavailable

    def post(self, url, **kw):
        return httpx.Response(502, json={"detail": {"error": "provider_rejected_request", "status": 401}},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.Client, "post", post)
    with pytest.raises(LLMUnavailable) as err:
        GatewayClient("http://gateway").complete("plan", [{"role": "user", "content": "x"}], run_id="r",
                                                 agent="research", step="s", prompt_version="v")
    assert err.value.reason == "provider_rejected_request:401"


def test_resource_exporter_uses_the_docker_proxy_url_when_configured(monkeypatch):
    from tenderscreen.llm import resource_exporter as rx

    monkeypatch.setattr(rx, "DOCKER_API_URL", "http://docker-proxy:2375")
    c = rx.client()
    try:
        assert str(c.base_url).startswith("http://docker-proxy:2375")
    finally:
        c.close()
