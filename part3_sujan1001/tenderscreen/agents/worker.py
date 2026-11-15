"""Queue consumer for one agent role. Each role runs in its own container.

    WORKER_ROLE=research   consumes stream:research, runs the research agent,
                           publishes the hand-off to stream:verify
    WORKER_ROLE=verifier   consumes stream:verify, verifies, then either
                           finalises the run or sends it back to
                           stream:research for another attempt

Failure handling (requirement A2):
  * a worker that dies mid-run never acknowledges its message; after
    QUEUE_LEASE_MS another worker of the same role reclaims it (XAUTOCLAIM)
    and redoes that stage from its input - stages are idempotent and a run
    that is already final is skipped
  * an exception inside a stage is retried by re-queueing (bounded); after
    QUEUE_MAX_DELIVERIES the message is dead-lettered and the run is marked
    failed - escalated, never returned as a confident answer
  * if the verifier service is down, hand-offs wait in stream:verify; the
    API reports the run as overdue once its deadline has passed
"""
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import time

from prometheus_client import start_http_server

from ..common import runstore
from ..common.config import get_settings
from ..common.logging import correlation_id, log, setup_logging
from ..common.queue import RESEARCH_STREAM, VERIFY_STREAM, StreamQueue
from ..common.redis_client import get_redis
from ..common.telemetry import attached_context, inject_context, setup_tracing, tracer
from ..llm.client import GatewayClient
from . import metrics as M
from .pipeline import RETRY_STRATEGY

S = get_settings()
ROLE = os.environ.get("WORKER_ROLE", "research")
HOST = socket.gethostname()
logger = setup_logging(f"{ROLE}-worker", S.log_level)
_running = True


def _stop(*_):
    global _running
    _running = False


def heartbeat(r, processed: int) -> None:
    r.set(f"worker:{ROLE}:{HOST}", json.dumps({"ts": time.time(), "processed": processed, "role": ROLE}), ex=20)


def finalize(run_id: str, result: dict, trace) -> None:
    run = runstore.get_run(run_id) or {}
    if run.get("status") in runstore.TERMINAL_STATUSES:
        return  # at-least-once delivery: first final result wins
    now = time.time()
    e2e = now - float(run.get("created_at", now))
    result["e2e_seconds"] = round(e2e, 3)
    runstore.set_status(run_id, "completed", result=result, completed_at=now, route=result["route"])
    M.E2E_SECONDS.labels(result["route"]).observe(e2e)
    M.RUNS_FINAL.labels(result["route"], str(result.get("verified")).lower(), str(result.get("degraded")).lower()).inc()
    trace.event(run_id, "final", agent=ROLE, route=result["route"], verified=result.get("verified"),
                degraded=result.get("degraded"), e2e_seconds=round(e2e, 3))
    key = (run.get("options") or {}).get("cache_key")
    if key and S.run_cache_enabled and result.get("verified") and not result.get("degraded") \
            and not (run.get("options") or {}).get("demo_fault"):
        get_redis().set(key, run_id, ex=3600)


def fail_run(run_id: str, reason: str, trace) -> None:
    result = {"run_id": run_id, "route": "escalate_to_human", "route_label": "Escalate to Tender Committee (human review)",
              "verified": False, "findings": [], "summary": "The run failed before a verified answer was produced.",
              "escalation": {"to": "Tender Committee (human review)", "reasons": [reason]}, "failed": True}
    runstore.set_status(run_id, "failed", result=result, completed_at=time.time(), route="escalate_to_human")
    M.RUNS_FINAL.labels("escalate_to_human", "false", "false").inc()
    trace.event(run_id, "failed", agent=ROLE, reason=reason)


def update_index_gauges(index) -> None:
    meta = index.meta
    M.INDEX_CHUNKS.set(meta.get("chunks", 0))
    M.INDEX_BUILT.set(meta.get("built_at", 0))
    M.INDEX_THROUGHPUT.set((meta.get("last_run") or {}).get("chunks_per_s", 0))
    M.INDEX_EMBEDDED.set((meta.get("last_run") or {}).get("chunks_embedded", 0))


def run_research() -> None:
    from ..retrieval.store import CorpusIndex, IndexNotReady
    from .research import ResearchAgent

    r = get_redis()
    trace = runstore.RedisTrace("research-worker")
    while True:
        try:
            index = CorpusIndex()
            break
        except IndexNotReady as exc:
            log(logger, logging.WARNING, "waiting for index", error=str(exc))
            time.sleep(3)
    update_index_gauges(index)
    agent = ResearchAgent(index, GatewayClient(S.llm_gateway_url, S.llm_call_timeout_s), trace, redis=r)
    inbox = StreamQueue(RESEARCH_STREAM, "research-workers")
    outbox = StreamQueue(VERIFY_STREAM, "verifiers")
    processed, last_beat = 0, 0.0
    log(logger, logging.INFO, "research worker ready", chunks=index.meta.get("chunks"))
    while _running:
        if time.time() - last_beat > 5:
            heartbeat(r, processed)
            update_index_gauges(index)
            M.QUEUE_DEPTH.labels("research").set(inbox.depth())
            last_beat = time.time()
        msg = inbox.consume(HOST, block_ms=2000)
        if msg is None:
            continue
        p = msg.payload
        run_id = p["run_id"]
        correlation_id.set(run_id)
        run = runstore.get_run(run_id)
        if not run or run.get("status") in runstore.TERMINAL_STATUSES:
            inbox.ack(msg.msg_id)
            continue
        if msg.deliveries > S.queue_max_deliveries or p.get("crashes", 0) >= S.queue_max_deliveries:
            fail_run(run_id, "research stage failed repeatedly (dead-lettered)", trace)
            inbox.dead_letter(msg, "max deliveries")
            continue
        if msg.deliveries > 1:
            trace.event(run_id, "redelivered", agent="research", deliveries=msg.deliveries, worker=HOST)
        with attached_context(p.get("trace_carrier") or run.get("trace_carrier")):
            with tracer("research-worker").start_as_current_span("worker.research") as span:
                span.set_attribute("run_id", run_id)
                runstore.set_status(run_id, "researching", attempt=p.get("attempt", 1), worker=HOST)
                job = {"run_id": run_id, "bid": run["bid"], "options": run.get("options", {}),
                       "attempt": p.get("attempt", 1), "strategy": p.get("strategy", "standard"),
                       "prior_failures": p.get("prior_failures", []), "created_at": run["created_at"]}
                try:
                    handoff = agent.run(job)
                except Exception as exc:  # noqa: BLE001
                    log(logger, logging.ERROR, "research stage crashed", error=repr(exc))
                    M.AGENT_TASKS.labels("research", "crash").inc()
                    inbox.publish({**p, "crashes": p.get("crashes", 0) + 1})
                    inbox.ack(msg.msg_id)
                    continue
                trace.event(run_id, "handoff", agent="research", to="verifier", attempt=job["attempt"], worker=HOST)
                runstore.set_status(run_id, "verifying", attempt=job["attempt"])
                outbox.publish({"run_id": run_id, "handoff": handoff, "trace_carrier": inject_context()})
        inbox.ack(msg.msg_id)
        processed += 1


def run_verifier() -> None:
    from ..evaluation.metrics import score
    from .verifier import VerifierAgent

    r = get_redis()
    trace = runstore.RedisTrace("verifier-worker")
    agent = VerifierAgent(trace)
    inbox = StreamQueue(VERIFY_STREAM, "verifiers")
    research = StreamQueue(RESEARCH_STREAM, "research-workers")
    processed, last_beat = 0, 0.0
    log(logger, logging.INFO, "verifier worker ready")
    while _running:
        if time.time() - last_beat > 5:
            heartbeat(r, processed)
            M.QUEUE_DEPTH.labels("verify").set(inbox.depth())
            last_beat = time.time()
        msg = inbox.consume(HOST, block_ms=2000)
        if msg is None:
            continue
        handoff = msg.payload["handoff"]
        run_id = handoff["run_id"]
        correlation_id.set(run_id)
        run = runstore.get_run(run_id)
        if not run or run.get("status") in runstore.TERMINAL_STATUSES:
            inbox.ack(msg.msg_id)
            continue
        if msg.deliveries > S.queue_max_deliveries:
            fail_run(run_id, "verification stage failed repeatedly (dead-lettered)", trace)
            inbox.dead_letter(msg, "max deliveries")
            continue
        with attached_context(msg.payload.get("trace_carrier")):
            with tracer("verifier-worker").start_as_current_span("worker.verify") as span:
                span.set_attribute("run_id", run_id)
                try:
                    decision, result = agent.verify(handoff)
                except Exception as exc:  # noqa: BLE001
                    log(logger, logging.ERROR, "verification crashed", error=repr(exc))
                    M.AGENT_TASKS.labels("verifier", "crash").inc()
                    fail_run(run_id, f"verifier error: {type(exc).__name__}", trace)
                    inbox.ack(msg.msg_id)
                    continue
                if decision == "retry":
                    failed = agent.failed_checks(result)
                    trace.event(run_id, "retry", agent="verifier", next_attempt=handoff["attempt"] + 1,
                                strategy=RETRY_STRATEGY, failed_checks=failed)
                    runstore.set_status(run_id, "retrying", attempt=handoff["attempt"] + 1)
                    research.publish({"run_id": run_id, "attempt": handoff["attempt"] + 1, "strategy": RETRY_STRATEGY,
                                      "prior_failures": failed, "trace_carrier": inject_context()})
                else:
                    finalize(run_id, result, trace)
                    if agent.sample(S.live_eval_sample_rate):
                        q = score(handoff)
                        row = {"ts": time.time(), "run_id": run_id, "route": result["route"], **q}
                        r.lpush("quality:samples", json.dumps(row))
                        r.ltrim("quality:samples", 0, 4999)
                        for k, v in q.items():
                            if v is not None:
                                M.LIVE_QUALITY.labels(k).set(v)
                                M.LIVE_QUALITY_HIST.labels(k).observe(v)
                        trace.event(run_id, "live_quality_sample", agent="verifier", **q)
        inbox.ack(msg.msg_id)
        processed += 1


def main() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    setup_tracing(f"{ROLE}-agent", S.otlp_endpoint)
    start_http_server(S.metrics_port)
    if ROLE == "research":
        run_research()
    elif ROLE == "verifier":
        run_verifier()
    else:
        raise SystemExit(f"unknown WORKER_ROLE {ROLE}")


if __name__ == "__main__":
    main()
