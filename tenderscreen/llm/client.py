"""What the agents use to call a model.

The agents never talk to a provider directly. They call the LLM gateway
(an interface we control), so the provider can be changed by configuration
and the breaker, retries, cache, budget and cost accounting live in one
place. ``InProcessStubClient`` gives the evaluation batch and unit tests the
same interface without any network.
"""
from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass

import httpx

from . import stub_logic


class LLMUnavailable(Exception):
    """The model could not be used (breaker open, provider down, timeout)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class LLMResult:
    content: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: float
    cached: bool
    provider: str
    model_alias: str


def price(prompt_tokens: int, completion_tokens: int) -> float:
    """List-price cost in USD at call time; prices are injected via env."""
    p_in = float(os.environ.get("LLM_PRICE_INPUT_PER_1M", "0") or 0)
    p_out = float(os.environ.get("LLM_PRICE_OUTPUT_PER_1M", "0") or 0)
    return round(prompt_tokens / 1e6 * p_in + completion_tokens / 1e6 * p_out, 8)


class GatewayClient:
    def __init__(self, base_url: str, timeout_s: float = 30.0, attempts: int = 2):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.attempts = attempts
        self.http = httpx.Client(timeout=timeout_s)

    def complete(self, task: str, messages: list[dict], *, run_id: str, agent: str, step: str,
                 prompt_version: str, max_tokens: int = 800, timeout_s: float | None = None) -> LLMResult:
        timeout = min(self.timeout_s, timeout_s) if timeout_s else self.timeout_s
        if timeout <= 0.5:
            raise LLMUnavailable("run_deadline_exhausted")
        body = {"task": task, "messages": messages, "max_tokens": max_tokens, "temperature": 0,
                "run_id": run_id, "agent": agent, "step": step, "prompt_version": prompt_version}
        last = "unknown"
        for attempt in range(self.attempts):
            t0 = time.time()
            try:
                resp = self.http.post(f"{self.base_url}/v1/complete", json=body, timeout=timeout,
                                      headers={"X-Correlation-ID": run_id})
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = f"gateway_unreachable:{type(exc).__name__}"
                time.sleep(min(0.5 * 2 ** attempt + random.random() * 0.2, 2.0))
                continue
            if resp.status_code == 200:
                d = resp.json()
                return LLMResult(d["content"], d["usage"]["prompt_tokens"], d["usage"]["completion_tokens"],
                                 d["cost_usd"], (time.time() - t0) * 1000, d.get("cached", False),
                                 d.get("provider", "?"), d.get("model_alias", "?"))
            try:
                err = resp.json().get("detail") or resp.json().get("error") or resp.text
            except ValueError:
                err = resp.text
            if isinstance(err, dict):
                err = err.get("error", str(err))
            # breaker open / budget exhausted / provider gave up: retrying here would only add latency
            raise LLMUnavailable(str(err)[:120])
        raise LLMUnavailable(last)


class InProcessStubClient:
    """Same contract as GatewayClient, answering with the stub logic."""

    def __init__(self, delay_s: float = 0.0, fail: bool = False):
        self.delay_s = delay_s
        self.fail = fail

    def complete(self, task: str, messages: list[dict], *, run_id: str, agent: str, step: str,
                 prompt_version: str, max_tokens: int = 800, timeout_s: float | None = None) -> LLMResult:
        if self.fail:
            raise LLMUnavailable("circuit_open")
        t0 = time.time()
        if self.delay_s:
            time.sleep(self.delay_s)
        content = stub_logic.respond(messages)
        pt = sum(stub_logic.estimate_tokens(m["content"]) for m in messages)
        ct = stub_logic.estimate_tokens(content)
        return LLMResult(content, pt, ct, price(pt, ct), (time.time() - t0) * 1000, False, "stub-inprocess", "stub")
