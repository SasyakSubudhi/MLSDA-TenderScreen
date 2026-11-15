"""Locust load test measuring COMPLETE runs (requirement D6).

Each simulated client submits a bid with randomised figures, polls until the
final result arrives, and records the whole run as one custom request named
"E2E complete run". Runs that end in a deadline escalation are counted as
failures. Think time between bids: 1-3 s.

    docker compose --profile loadtest run --rm locust --headless -u 10 -r 2 -t 2m \
        --host http://api:8000 --csv results/w1_u10
"""
from __future__ import annotations

import glob
import json
import os
import random
import time
import uuid

from locust import HttpUser, between, events, task

EXAMPLES_DIR = os.environ.get("EXAMPLES_DIR", "/mnt/examples")
VALID = ["ordinary_pass", "startup_low_turnover", "large_low_turnover_reject", "mse_emd_exempt_pass",
         "missing_documents_clarify", "consortium_cannot_answer"]
BIDS = {}
for path in glob.glob(os.path.join(EXAMPLES_DIR, "*.json")):
    name = os.path.basename(path)[:-5]
    if name in VALID:
        with open(path, encoding="utf-8") as fh:
            BIDS[name] = json.load(fh)
POLL_S = float(os.environ.get("POLL_S", "1.0"))
MAX_WAIT_S = float(os.environ.get("MAX_WAIT_S", "150"))
RANDOMISE = os.environ.get("RANDOMISE", "1") == "1"


def make_bid() -> dict:
    name = random.choice(list(BIDS))
    bid = json.loads(json.dumps(BIDS[name]))
    bid["bid_id"] = "LT-" + uuid.uuid4().hex[:12]
    if RANDOMISE:  # distinct figures -> different prompts -> no cache can hide the work
        for fy in bid["financials"]["annual_turnover_inr"]:
            v = bid["financials"]["annual_turnover_inr"][fy]
            bid["financials"]["annual_turnover_inr"][fy] = round(v * random.uniform(0.9, 1.1), 0)
    return bid


class Screener(HttpUser):
    wait_time = between(1, 3)

    @task
    def screen_one_bid(self):
        t0 = time.time()
        with self.client.post("/v1/screenings", json=make_bid(), headers={"X-Client-Id": "locust"},
                              name="POST /v1/screenings", catch_response=True) as resp:
            if resp.status_code not in (200, 202):
                resp.failure(f"submit HTTP {resp.status_code}")
                return
            run_id = resp.json()["run_id"]
        result, exc = None, None
        while time.time() - t0 < MAX_WAIT_S:
            time.sleep(POLL_S)
            r = self.client.get(f"/v1/screenings/{run_id}", name="GET /v1/screenings/[id]")
            if r.status_code == 200 and r.json().get("result"):
                result = r.json()["result"]
                break
        if result is None:
            exc = Exception("no result before MAX_WAIT_S")
        elif result.get("incomplete") or result.get("failed") or any(
                "deadline" in str(x) for x in (result.get("escalation") or {}).get("reasons", [])):
            exc = Exception("run hit its deadline or failed")
        events.request.fire(request_type="E2E", name="complete run", response_time=(time.time() - t0) * 1000,
                            response_length=0, exception=exc, context={})
