"""Part 1 - data contract, ingestion boundary, quarantine, rate limit, async API."""
import copy

import pytest
from fastapi.testclient import TestClient

from tenderscreen.api.contracts import validate_bid


def test_valid_examples_pass_the_contract(example):
    for name in ("ordinary_pass", "startup_low_turnover", "mse_emd_exemption_conflict", "consortium_cannot_answer",
                 "outside_region_reject"):
        bid, errors = validate_bid(example(name))
        assert bid is not None and errors == [], (name, errors)


def test_malformed_bid_gets_field_level_errors(example):
    bid, errors = validate_bid(example("malformed"))
    assert bid is None
    fields = {e["field"] for e in errors}
    assert "tender_id" in fields and "bidder.registered_office_state" in fields
    assert {e["error"] for e in errors} <= {"schema", "range", "categorical", "type", "cross_field"}


@pytest.mark.parametrize("mutate, field", [
    (lambda b: b["financials"]["annual_turnover_inr"].pop("2017-18"), "financials.annual_turnover_inr"),
    (lambda b: b["bidder"].update(declared_category="startup", dpiit_recognition=None), "bidder.dpiit_recognition"),
    (lambda b: b["bidder"].update(date_of_incorporation="2026-01-01"), "bidder.date_of_incorporation"),
    (lambda b: b["emd"].update(amount_inr=0), "emd.amount_inr"),
    (lambda b: b["experience"].update(years_in_desktop_business=40), "experience.years_in_desktop_business"),
    (lambda b: b["bidder"].update(country_of_incorporation="SG"), "bidder.country_of_incorporation"),
    (lambda b: b["bidder"].update(registered_office_state="Outside India"), "bidder.registered_office_state"),
    (lambda b: b["bidder"].update(consortium_members=["A Ltd", "B Ltd"]), "bidder.consortium_members"),
])
def test_cross_field_rules(example, mutate, field):
    bid = copy.deepcopy(example("ordinary_pass"))
    mutate(bid)
    ok, errors = validate_bid(bid)
    assert ok is None
    assert field in {e["field"] for e in errors}, errors


def test_unknown_fields_are_rejected(example):
    bid = example("ordinary_pass")
    bid["approve"] = True
    assert validate_bid(bid)[0] is None


@pytest.fixture()
def api(fake_redis, monkeypatch):
    import tenderscreen.api.main as main
    import tenderscreen.api.ratelimit as rl
    import tenderscreen.common.queue as q
    import tenderscreen.common.runstore as rs

    for mod in (main, rl, rs, q):
        monkeypatch.setattr(mod, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(main, "_queue", None)
    return TestClient(main.app), fake_redis


def test_submit_returns_run_id_immediately_and_enqueues(api, example):
    client, r = api
    resp = client.post("/v1/screenings", json=example("ordinary_pass"))
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued" and len(body["run_id"]) == 16
    assert r.xlen("stream:research") == 1
    status = client.get(body["status_url"]).json()
    assert status["status"] == "queued"


def test_malformed_request_is_quarantined_and_no_run_starts(api, example):
    client, r = api
    resp = client.post("/v1/screenings", json=example("malformed"))
    assert resp.status_code == 422
    body = resp.json()
    assert body["agent_run_started"] is False and body["quarantine_id"].startswith("q_")
    assert r.xlen("stream:research") == 0 and not list(r.scan_iter("run:*"))
    q = client.get("/v1/quarantine").json()
    assert q["count"] == 1 and q["items"][0]["errors"]


def test_prompt_injection_is_quarantined(api, example):
    client, r = api
    resp = client.post("/v1/screenings", json=example("prompt_injection"))
    assert resp.status_code == 422
    assert resp.json()["reason"] == "prompt_injection"
    assert r.xlen("stream:research") == 0


def test_rate_limit_per_client(api, example):
    client, _ = api
    codes = []
    for i in range(7):
        bid = example("ordinary_pass")
        bid["bid_id"] = f"RL-{i}"
        codes.append(client.post("/v1/screenings", json=bid, headers={"X-Client-Id": "looper"}).status_code)
    assert codes[:5] == [202] * 5 and codes[5] == 429
    other = client.post("/v1/screenings", json=example("ordinary_pass"), headers={"X-Client-Id": "someone-else"})
    assert other.status_code == 202


def test_contract_and_web_client_are_served(api):
    client, _ = api
    c = client.get("/v1/contract").json()
    assert c["version"] == "1.0" and c["cross_field_rules"]
    assert "TenderScreen" in client.get("/").text
