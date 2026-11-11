"""Public REST API (requirements F1, F2, A4, D1, D7, L6).

Asynchronous by design: a screening takes tens of seconds, so
``POST /v1/screenings`` validates, quarantines or enqueues, and returns a run
identifier immediately (HTTP 202). The caller polls
``GET /v1/screenings/{run_id}``. Polling was chosen over callbacks because
the main client is a browser page, which cannot receive a callback, and
because polling needs no inbound network path to the caller. A Redis Streams
queue sits between submission and execution.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from ..agents.guardrails import detect_toxicity, scan_bid
from ..agents.prompts import version_tag
from ..common import runstore
from ..common.config import get_settings
from ..common.logging import correlation_id, log, setup_logging
from ..common.queue import RESEARCH_STREAM, VERIFY_STREAM, StreamQueue
from ..common.redis_client import get_redis
from ..common.telemetry import inject_context, setup_tracing, tracer
from .contracts import CROSS_FIELD_RULES, FeedbackIn, json_schema, validate_bid
from .ratelimit import check_rate_limit

S = get_settings()
logger = setup_logging("api", S.log_level)
setup_tracing("api", S.otlp_endpoint)
STATIC = os.path.join(os.path.dirname(__file__), "static")
GATEWAY = S.llm_gateway_url.rstrip("/")
STUB_URL = os.environ.get("LLM_STUB_ADMIN_URL", "http://llm-stub:8200").rstrip("/")

HTTP_REQS = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LAT = Histogram("http_request_seconds", "HTTP latency", ["method", "route"],
                     buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5))
SUBMITTED = Counter("runs_submitted_total", "Runs accepted and queued")
QUARANTINED = Counter("requests_quarantined_total", "Requests rejected at ingestion", ["reason"])
RATE_LIMITED = Counter("requests_rate_limited_total", "Requests refused by the rate limiter")
RUN_CACHE = Counter("run_cache_total", "Whole-run cache lookups", ["result"])
FEEDBACK = Counter("feedback_total", "Feedback received", ["satisfactory"])
QUEUE_DEPTH = Gauge("api_queue_depth", "Messages in stream", ["stream"])

app = FastAPI(title="TenderScreen - Bid Compliance Screening API", version="1.0",
              description="Screens bid summaries for TENDER/2024-25/370 and returns a cited screening finding. "
                          "It never qualifies or rejects a bid: the Tender Committee decides.")
try:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")
except Exception:  # pragma: no cover
    pass

_queue: StreamQueue | None = None


def queue() -> StreamQueue:
    global _queue
    if _queue is None:
        _queue = StreamQueue(RESEARCH_STREAM, "research-workers")
    return _queue


def client_id(request: Request) -> str:
    cid = request.headers.get("x-client-id", "")
    if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", cid or ""):
        return cid
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def observe(request: Request, call_next):
    cid = request.headers.get("x-correlation-id") or "-"
    correlation_id.set(cid)
    t0 = time.time()
    response = await call_next(request)
    route = request.scope.get("route")
    path = getattr(route, "path", "unmatched")
    HTTP_REQS.labels(request.method, path, str(response.status_code)).inc()
    HTTP_LAT.labels(request.method, path).observe(time.time() - t0)
    return response


def _index_meta() -> dict:
    try:
        with open(os.path.join(S.index_dir, "meta.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _quarantine(raw, errors: list[dict], client: str, reason: str) -> str:
    qid = "q_" + uuid.uuid4().hex[:12]
    record = {"quarantine_id": qid, "ts": time.time(), "client_id": client, "reason": reason,
              "errors": errors, "payload": raw if isinstance(raw, (dict, list)) else str(raw)[:2000],
              "agent_run_started": False}
    r = get_redis()
    r.lpush("quarantine", json.dumps(record, default=str))
    r.ltrim("quarantine", 0, 999)
    QUARANTINED.labels(reason).inc()
    log(logger, logging.WARNING, "request quarantined", quarantine_id=qid, reason=reason, errors=len(errors))
    return qid


def _run_cache_key(bid: dict, options: dict) -> str:
    raw = json.dumps({"bid": bid, "pv": version_tag(options["prompt_version"]),
                      "ix": _index_meta().get("index_hash", "")}, sort_keys=True)
    return "runcache:" + hashlib.sha256(raw.encode()).hexdigest()


# ===================================================================== screening
@app.post("/v1/screenings", status_code=202, tags=["screening"])
async def submit(request: Request):
    """Submit a bid summary (see GET /v1/contract). Returns a run id at once."""
    client = client_id(request)
    allowed, retry_after = check_rate_limit(client, S.rate_limit_per_min)
    if not allowed:
        RATE_LIMITED.inc()
        return JSONResponse(status_code=429, headers={"Retry-After": str(retry_after)},
                            content={"error": "rate_limited", "limit_per_minute": S.rate_limit_per_min,
                                     "retry_after_s": retry_after})
    try:
        raw = await request.json()
    except (ValueError, UnicodeDecodeError):
        qid = _quarantine((await request.body()).decode("utf-8", "replace"),
                          [{"field": "$", "error": "schema", "message": "body is not valid JSON"}], client, "invalid_json")
        return JSONResponse(status_code=422, content={"error": "validation_failed", "quarantine_id": qid,
                                                      "agent_run_started": False,
                                                      "errors": [{"field": "$", "message": "body is not valid JSON"}]})
    with tracer("api").start_as_current_span("ingest.validate") as span:
        bid, errors = validate_bid(raw)
        reason = "contract_violation"
        if bid is not None:
            errors = scan_bid(bid.model_dump(mode="json"))
            if errors:
                reason = "prompt_injection" if any(e["error"] == "prompt_injection_detected" for e in errors) else "toxicity"
        span.set_attribute("validation.errors", len(errors))
    if errors:
        qid = _quarantine(raw, errors, client, reason)
        return JSONResponse(status_code=422, content={"error": "validation_failed", "reason": reason,
                                                      "quarantine_id": qid, "agent_run_started": False,
                                                      "errors": errors})

    options = {"prompt_version": S.prompt_version}
    pv = request.query_params.get("prompt_version")
    if pv in ("v1", "v2"):
        options["prompt_version"] = pv
    fault = request.query_params.get("demo_fault")
    if fault and S.demo_mode and fault in ("unsupported_citation",):
        options["demo_fault"] = fault
    bid_dict = bid.model_dump(mode="json")

    r = get_redis()
    ckey = _run_cache_key(bid_dict, options)
    if S.run_cache_enabled and "demo_fault" not in options:
        prior = r.get(ckey)
        if prior:
            run = runstore.get_run(prior)
            if run and run.get("status") == "completed":
                RUN_CACHE.labels("hit").inc()
                return JSONResponse(status_code=200, content={
                    "run_id": prior, "status": "completed", "cached": True,
                    "status_url": f"/v1/screenings/{prior}", "trace_url": f"/v1/screenings/{prior}/trace"})
        RUN_CACHE.labels("miss").inc()

    run_id = uuid.uuid4().hex[:16]
    correlation_id.set(run_id)
    with tracer("api").start_as_current_span("screening.submit") as span:
        span.set_attribute("run_id", run_id)
        carrier = inject_context()
        runstore.create_run(run_id, bid_dict, {**options, "cache_key": ckey}, client, carrier)
        trace = runstore.RedisTrace("api")
        trace.event(run_id, "submitted", agent="api", client_id=client, bid_id=bid_dict["bid_id"],
                    prompt_version=version_tag(options["prompt_version"]), options=options)
        queue().publish({"run_id": run_id, "attempt": 1, "strategy": "standard", "trace_carrier": carrier})
    SUBMITTED.inc()
    log(logger, logging.INFO, "run queued", bid_id=bid_dict["bid_id"], client_id=client)
    return {"run_id": run_id, "status": "queued", "cached": False, "status_url": f"/v1/screenings/{run_id}",
            "trace_url": f"/v1/screenings/{run_id}/trace", "poll_after_ms": 1000}


@app.get("/v1/screenings/{run_id}", tags=["screening"])
def status(run_id: str):
    if not re.fullmatch(r"[a-f0-9]{16}", run_id):
        return JSONResponse(status_code=404, content={"error": "unknown run id"})
    run = runstore.get_run(run_id)
    if not run:
        return JSONResponse(status_code=404, content={"error": "unknown run id"})
    elapsed = (run.get("completed_at") or time.time()) - run["created_at"]
    out = {"run_id": run_id, "status": run["status"], "created_at": run["created_at"],
           "elapsed_s": round(elapsed, 2), "attempt": int(run.get("attempt", 1) or 1)}
    if run["status"] in runstore.TERMINAL_STATUSES:
        out["result"] = run.get("result")
    elif elapsed > S.run_timeout_s * 1.5:
        out["overdue"] = True
        out["note"] = "run is past its deadline; it will be returned as an escalation, never as a confident answer"
    return out


@app.get("/v1/screenings/{run_id}/trace", tags=["observability"])
def run_trace(run_id: str):
    """Everything needed to reconstruct a run: every tool call, query, passage,
    prompt version, per-step latency, tokens, cost and verification outcome."""
    run = runstore.get_run(run_id) if re.fullmatch(r"[a-f0-9]{16}", run_id) else None
    if not run:
        return JSONResponse(status_code=404, content={"error": "unknown run id"})
    events = runstore.get_events(run_id)
    llm = [e for e in events if e["type"] == "llm_call"]
    tools = [e for e in events if e["type"] == "tool_call"]
    per_step: dict[str, dict] = {}
    for e in llm + tools:
        key = f"{e.get('agent')}:{e.get('task') or e.get('tool')}"
        d = per_step.setdefault(key, {"calls": 0, "latency_ms": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
                                      "cost_usd": 0.0})
        d["calls"] += 1
        d["latency_ms"] = round(d["latency_ms"] + float(e.get("latency_ms") or 0), 1)
        d["prompt_tokens"] += int(e.get("prompt_tokens") or 0)
        d["completion_tokens"] += int(e.get("completion_tokens") or 0)
        d["cost_usd"] = round(d["cost_usd"] + float(e.get("cost_usd") or 0), 8)
    summary = {
        "tool_calls": len(tools), "llm_calls": len(llm),
        "queries": [e["args"].get("query") for e in tools if e["tool"] == "search_corpus"],
        "prompt_versions": sorted({e.get("prompt_version") for e in events if e.get("prompt_version")}),
        "prompt_tokens": sum(int(e.get("prompt_tokens") or 0) for e in llm),
        "completion_tokens": sum(int(e.get("completion_tokens") or 0) for e in llm),
        "cost_usd": round(sum(float(e.get("cost_usd") or 0) for e in llm), 8),
        "per_step": per_step,
        "verification": [{"attempt": e.get("attempt"), "decision": e.get("decision")}
                         for e in events if e["type"] == "verification"],
    }
    bid = run.get("bid")
    run_public = {k: v for k, v in run.items() if k not in ("bid", "trace_carrier")}
    return {"run": run_public, "bid": bid, "summary": summary, "events": events}


@app.post("/v1/screenings/{run_id}/feedback", tags=["feedback"])
async def feedback(run_id: str, request: Request):
    run = runstore.get_run(run_id) if re.fullmatch(r"[a-f0-9]{16}", run_id) else None
    if not run:
        return JSONResponse(status_code=404, content={"error": "unknown run id"})
    try:
        fb = FeedbackIn.model_validate(await request.json())
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=422, content={"error": "invalid feedback", "detail": str(exc)[:300]})
    if detect_toxicity(fb.reason):
        return JSONResponse(status_code=422, content={"error": "toxic_content_detected"})
    route = (run.get("result") or {}).get("route", "unknown")
    rec = {"run_id": run_id, "ts": time.time(), "satisfactory": fb.satisfactory, "reason": fb.reason, "route": route,
           "client_id": client_id(request)}
    r = get_redis()
    r.lpush("feedback:all", json.dumps(rec))
    r.ltrim("feedback:all", 0, 9999)
    r.hincrby("feedback:counts", "satisfactory" if fb.satisfactory else "unsatisfactory", 1)
    r.hincrby("feedback:by_route", f"{route}:{'yes' if fb.satisfactory else 'no'}", 1)
    runstore.RedisTrace("api").event(run_id, "feedback", agent="api", satisfactory=fb.satisfactory, reason=fb.reason)
    FEEDBACK.labels(str(fb.satisfactory).lower()).inc()
    return {"stored": True, **rec}


@app.get("/v1/feedback/summary", tags=["feedback"])
def feedback_summary():
    r = get_redis()
    counts = {k: int(v) for k, v in r.hgetall("feedback:counts").items()}
    by_route = {k: int(v) for k, v in r.hgetall("feedback:by_route").items()}
    total = sum(counts.values())
    recent = [json.loads(x) for x in r.lrange("feedback:all", 0, 9)]
    return {"total": total, "counts": counts,
            "satisfaction_rate": round(counts.get("satisfactory", 0) / total, 3) if total else None,
            "by_route": by_route, "recent": recent}


@app.get("/v1/quarantine", tags=["observability"])
def quarantine(limit: int = 20):
    r = get_redis()
    items = [json.loads(x) for x in r.lrange("quarantine", 0, max(0, min(limit, 200)) - 1)]
    return {"count": r.llen("quarantine"), "items": items}


@app.get("/v1/quality", tags=["observability"])
def quality(limit: int = 50):
    rows = [json.loads(x) for x in get_redis().lrange("quality:samples", 0, max(1, min(limit, 500)) - 1)]
    return {"samples": rows}


# ===================================================================== contract & system
@app.get("/v1/contract", tags=["contract"])
def contract():
    return {"name": "BidSummary", "version": "1.0", "json_schema": json_schema(),
            "cross_field_rules": CROSS_FIELD_RULES,
            "responses": {"202": "queued - poll status_url", "200": "served from the whole-run cache",
                          "422": "quarantined - field-level errors returned, no agent run started",
                          "429": "rate limited (per client)"}}


@app.get("/v1/examples", tags=["contract"])
def examples():
    out = {}
    for name in sorted(os.listdir(S.examples_dir)):
        if name.endswith(".json"):
            with open(os.path.join(S.examples_dir, name), encoding="utf-8") as fh:
                out[name[:-5]] = json.load(fh)
    return out


@app.get("/v1/system", tags=["observability"])
def system():
    r = get_redis()
    workers = {}
    for key in r.scan_iter("worker:*", count=200):
        try:
            workers[key[7:]] = json.loads(r.get(key) or "{}")
        except ValueError:
            pass
    try:
        gw = httpx.get(f"{GATEWAY}/health", timeout=2).json()
    except Exception as exc:  # noqa: BLE001
        gw = {"status": "unreachable", "error": type(exc).__name__}
    meta = _index_meta()
    return {
        "llm_gateway": gw,
        "index": {"built_at": meta.get("built_at"), "age_s": round(time.time() - meta["built_at"], 1) if meta else None,
                  "chunks": meta.get("chunks"), "embedding_backend": meta.get("embedding_backend"),
                  "corpus_version": (meta.get("corpus") or {}).get("corpus_version"),
                  "documents": (meta.get("corpus") or {}).get("documents"), "last_run": meta.get("last_run")},
        "queues": {"research": StreamQueue(RESEARCH_STREAM, "research-workers").depth(),
                   "verify": StreamQueue(VERIFY_STREAM, "verifiers").depth()},
        "workers": workers,
        "prompt_version": version_tag(S.prompt_version),
        "demo_mode": S.demo_mode,
        "limits": {"max_steps": S.max_steps, "max_tool_calls": S.max_tool_calls, "run_timeout_s": S.run_timeout_s,
                   "rate_limit_per_min": S.rate_limit_per_min},
    }


@app.post("/v1/admin/provider-mode", tags=["demo"])
async def provider_mode(request: Request):
    """Demo only: switch the stub provider to ok|down|slow|flaky (circuit-breaker demo)."""
    if not S.demo_mode:
        return JSONResponse(status_code=403, content={"error": "demo mode disabled"})
    body = await request.json()
    try:
        resp = httpx.post(f"{STUB_URL}/admin/mode", json={"mode": body.get("mode")}, timeout=3)
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=502, content={"error": f"stub unreachable: {type(exc).__name__}"})


@app.post("/v1/admin/breaker-reset", tags=["demo"])
def breaker_reset():
    if not S.demo_mode:
        return JSONResponse(status_code=403, content={"error": "demo mode disabled"})
    try:
        return httpx.post(f"{GATEWAY}/admin/breaker/reset", timeout=3).json()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=502, content={"error": f"gateway unreachable: {type(exc).__name__}"})


@app.get("/health", tags=["observability"])
def health():
    try:
        get_redis().ping()
        return {"status": "ok", "service": "api"}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"status": "unhealthy", "redis": type(exc).__name__})


@app.get("/metrics", include_in_schema=False)
def metrics():
    try:
        QUEUE_DEPTH.labels("research").set(StreamQueue(RESEARCH_STREAM, "research-workers").depth())
        QUEUE_DEPTH.labels("verify").set(StreamQueue(VERIFY_STREAM, "verifiers").depth())
    except Exception:  # noqa: BLE001
        pass
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))
