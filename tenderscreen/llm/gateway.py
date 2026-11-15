"""LLM gateway service - the only component that talks to a model provider.

    agents --HTTP--> llm-gateway --HTTP--> provider (llm-stub, or any
                                            OpenAI-compatible free tier)

Responsibilities (requirements A5, L5, D7):
  * timeout on every provider call
  * bounded retry with exponential backoff + jitter on retryable errors
  * circuit breaker shared by every agent worker
  * response cache (Redis) - identical prompts are answered without a call
  * daily budget cap - a looping client cannot exhaust the account
  * prompt/completion tokens and cost recorded at call time, labelled by
    agent, task (step) and prompt version

Provider selection is configuration only: LLM_BASE_URL / LLM_API_KEY /
LLM_MODEL. Nothing provider-specific is in the code.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import time

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from opentelemetry import trace
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field

from ..common.config import get_settings
from ..common.logging import correlation_id, log, setup_logging
from ..common.redis_client import get_redis
from ..common.telemetry import setup_tracing, tracer
from .breaker import STATE_CODES, CircuitBreaker
from .client import price
from .stub_logic import estimate_tokens

S = get_settings()
logger = setup_logging("llm-gateway", S.log_level)
setup_tracing("llm-gateway", S.otlp_endpoint)

BASE_URL = os.environ.get("LLM_BASE_URL", "http://llm-stub:8200/v1").rstrip("/")
API_KEY = os.environ.get("LLM_API_KEY", "")
MODEL = os.environ.get("LLM_MODEL", "")
PROVIDER_NAME = os.environ.get("LLM_PROVIDER_NAME", "stub")
PROVIDER_TIMEOUT_S = float(os.environ.get("LLM_PROVIDER_TIMEOUT_S", "20"))
MAX_ATTEMPTS = int(os.environ.get("LLM_MAX_ATTEMPTS", "3"))
BACKOFF_BASE_S = float(os.environ.get("LLM_BACKOFF_BASE_S", "0.5"))
CACHE_ENABLED = os.environ.get("LLM_CACHE_ENABLED", "true").lower() == "true"
CACHE_TTL_S = int(os.environ.get("LLM_CACHE_TTL_S", "86400"))
DAILY_BUDGET_USD = float(os.environ.get("LLM_DAILY_BUDGET_USD", "1.0"))

breaker = CircuitBreaker(int(os.environ.get("BREAKER_FAILURE_THRESHOLD", "5")),
                         float(os.environ.get("BREAKER_OPEN_SECONDS", "20")))

REQS = Counter("llm_requests_total", "Gateway requests", ["task", "agent", "outcome"])
ATTEMPTS = Counter("llm_provider_attempts_total", "Provider call attempts", ["outcome"])
RETRIES = Counter("llm_retries_total", "Provider retries after a retryable error")
LAT = Histogram("llm_call_seconds", "Gateway call latency", ["task", "agent"],
                buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32))
TOKENS = Counter("llm_tokens_total", "Tokens", ["type", "agent", "task", "prompt_version"])
COST = Counter("llm_cost_usd_total", "List-price cost in USD", ["agent", "task", "prompt_version"])
CACHE = Counter("llm_cache_total", "Response cache lookups", ["result"])
BREAKER_STATE = Gauge("llm_breaker_state", "0=closed 1=half_open 2=open")
BREAKER_OPENS = Gauge("llm_breaker_opens_total", "Times the breaker opened")

app = FastAPI(title="TenderScreen LLM gateway", version="1.0")
try:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")
except Exception:  # pragma: no cover
    pass

_http = httpx.AsyncClient(timeout=PROVIDER_TIMEOUT_S)


class CompleteRequest(BaseModel):
    task: str = Field(max_length=40)
    messages: list[dict] = Field(min_length=1, max_length=20)
    max_tokens: int = Field(default=800, ge=1, le=4000)
    temperature: float = Field(default=0.0, ge=0, le=2)
    run_id: str = "-"
    agent: str = "-"
    step: str = "-"
    prompt_version: str = "-"


def _budget_key() -> str:
    return "llm:spend:" + time.strftime("%Y-%m-%d", time.gmtime())


def _spent_today() -> float:
    try:
        return float(get_redis().get(_budget_key()) or 0)
    except Exception:
        return 0.0


def _cache_key(req: CompleteRequest) -> str:
    raw = json.dumps({"m": MODEL, "p": PROVIDER_NAME, "t": req.temperature, "x": req.max_tokens,
                      "msgs": req.messages}, sort_keys=True)
    return "llm:cache:" + hashlib.sha256(raw.encode()).hexdigest()


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


async def _call_provider(req: CompleteRequest) -> dict:
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    body = {"model": MODEL, "messages": req.messages, "max_tokens": req.max_tokens, "temperature": req.temperature}
    last_error = "unknown"
    for attempt in range(MAX_ATTEMPTS):
        if not breaker.allow():
            raise HTTPException(503, {"error": "circuit_open", "retry_after_s": breaker.retry_after()})
        try:
            resp = await _http.post(f"{BASE_URL}/chat/completions", json=body, headers=headers,
                                    timeout=PROVIDER_TIMEOUT_S)
            if resp.status_code == 200:
                breaker.record_success()
                ATTEMPTS.labels("ok").inc()
                return resp.json()
            last_error = f"http_{resp.status_code}"
            if not _retryable(resp.status_code):
                # our request was wrong; the provider is healthy - do not trip the breaker
                ATTEMPTS.labels("client_error").inc()
                raise HTTPException(502, {"error": "provider_rejected_request", "status": resp.status_code,
                                          "body": resp.text[:300]})
        except httpx.TimeoutException:
            last_error = "timeout"
        except httpx.TransportError as exc:
            last_error = f"transport:{type(exc).__name__}"
        ATTEMPTS.labels("error").inc()
        breaker.record_failure()
        if attempt < MAX_ATTEMPTS - 1:
            RETRIES.inc()
            await asyncio.sleep(BACKOFF_BASE_S * (2 ** attempt) + random.uniform(0, BACKOFF_BASE_S / 2))
    raise HTTPException(503, {"error": "provider_unavailable", "last_error": last_error})


@app.post("/v1/complete")
async def complete(req: CompleteRequest, request: Request):
    correlation_id.set(req.run_id)
    t0 = time.time()
    span = trace.get_current_span()
    span.set_attribute("run_id", req.run_id)
    span.set_attribute("llm.task", req.task)
    span.set_attribute("llm.agent", req.agent)

    if _spent_today() >= DAILY_BUDGET_USD:
        REQS.labels(req.task, req.agent, "budget_exhausted").inc()
        raise HTTPException(503, {"error": "daily_budget_exhausted"})

    key = _cache_key(req)
    if CACHE_ENABLED:
        try:
            hit = get_redis().get(key)
        except Exception:
            hit = None
        if hit:
            CACHE.labels("hit").inc()
            REQS.labels(req.task, req.agent, "cache_hit").inc()
            d = json.loads(hit)
            d.update(cached=True, cost_usd=0.0, latency_ms=round((time.time() - t0) * 1000, 1))
            return d
        CACHE.labels("miss").inc()

    with tracer("llm-gateway").start_as_current_span("provider.chat_completions") as pspan:
        pspan.set_attribute("provider", PROVIDER_NAME)
        try:
            data = await _call_provider(req)
        except HTTPException as exc:
            outcome = exc.detail.get("error", "error") if isinstance(exc.detail, dict) else "error"
            REQS.labels(req.task, req.agent, outcome).inc()
            BREAKER_STATE.set(STATE_CODES[breaker.state])
            BREAKER_OPENS.set(breaker.opens_total)
            log(logger, logging.WARNING, "llm call failed", task=req.task, outcome=outcome, breaker=breaker.state)
            raise
    BREAKER_STATE.set(STATE_CODES[breaker.state])

    content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    usage = data.get("usage") or {}
    pt = int(usage.get("prompt_tokens") or sum(estimate_tokens(m.get("content", "")) for m in req.messages))
    ct = int(usage.get("completion_tokens") or estimate_tokens(content))
    cost = price(pt, ct)
    elapsed = time.time() - t0
    TOKENS.labels("prompt", req.agent, req.task, req.prompt_version).inc(pt)
    TOKENS.labels("completion", req.agent, req.task, req.prompt_version).inc(ct)
    COST.labels(req.agent, req.task, req.prompt_version).inc(cost)
    LAT.labels(req.task, req.agent).observe(elapsed)
    REQS.labels(req.task, req.agent, "ok").inc()
    out = {"content": content, "usage": {"prompt_tokens": pt, "completion_tokens": ct}, "cost_usd": cost,
           "provider": PROVIDER_NAME, "model_alias": "configured-model", "cached": False,
           "latency_ms": round(elapsed * 1000, 1)}
    try:
        r = get_redis()
        r.incrbyfloat(_budget_key(), cost)
        r.expire(_budget_key(), 2 * 86400)
        if CACHE_ENABLED:
            r.set(key, json.dumps(out), ex=CACHE_TTL_S)
    except Exception:
        pass
    log(logger, logging.INFO, "llm call", task=req.task, agent=req.agent, step=req.step, prompt_tokens=pt,
        completion_tokens=ct, cost_usd=cost, latency_ms=out["latency_ms"])
    return out


@app.get("/health")
def health():
    snap = breaker.snapshot()
    BREAKER_STATE.set(STATE_CODES[breaker.state])
    return {"status": "ok", "provider": PROVIDER_NAME, "breaker": snap, "cache_enabled": CACHE_ENABLED,
            "spent_today_usd": round(_spent_today(), 6), "daily_budget_usd": DAILY_BUDGET_USD}


@app.post("/admin/breaker/reset")
def reset_breaker():
    breaker.reset()
    BREAKER_STATE.set(0)
    return breaker.snapshot()


@app.get("/metrics")
def metrics():
    BREAKER_STATE.set(STATE_CODES[breaker.state])
    BREAKER_OPENS.set(breaker.opens_total)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
