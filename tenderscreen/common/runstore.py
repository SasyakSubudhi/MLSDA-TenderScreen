"""Run state and the per-run event trace.

Every run has:
    run:{id}          hash   status, timestamps, bid, options, result
    run:{id}:events   list   one JSON event per tool call, LLM call, step,
                             hand-off and verification outcome

The event list is what makes a run reconstructible from its identifier
(requirement D5): every tool call, query, passage, prompt version,
per-step latency, token count, cost and the verification outcome.

``TraceRecorder`` is the small interface the agents write to. In the
containers it is backed by Redis (``RedisTrace``); the evaluation batch and
unit tests use ``MemoryTrace`` so they run without any infrastructure.
"""
from __future__ import annotations

import json
import time
from typing import Any, Protocol

from .redis_client import get_redis

RUN_TTL_S = 7 * 24 * 3600
TERMINAL_STATUSES = {"completed", "failed"}


class TraceRecorder(Protocol):
    def event(self, run_id: str, type_: str, **data: Any) -> None: ...


class MemoryTrace:
    def __init__(self) -> None:
        self.events: dict[str, list[dict]] = {}

    def event(self, run_id: str, type_: str, **data: Any) -> None:
        self.events.setdefault(run_id, []).append({"ts": time.time(), "type": type_, **data})

    def get(self, run_id: str) -> list[dict]:
        return self.events.get(run_id, [])


class RedisTrace:
    def __init__(self, service: str):
        self.service = service
        self.r = get_redis()

    def event(self, run_id: str, type_: str, **data: Any) -> None:
        payload = {"ts": time.time(), "service": self.service, "type": type_, **data}
        key = f"run:{run_id}:events"
        pipe = self.r.pipeline()
        pipe.rpush(key, json.dumps(payload, default=str))
        pipe.expire(key, RUN_TTL_S)
        pipe.execute()


# ---------------------------------------------------------------- run record
def create_run(run_id: str, bid: dict, options: dict, client_id: str, trace_carrier: dict) -> None:
    r = get_redis()
    now = time.time()
    r.hset(
        f"run:{run_id}",
        mapping={
            "run_id": run_id,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "client_id": client_id,
            "bid": json.dumps(bid),
            "options": json.dumps(options),
            "trace_carrier": json.dumps(trace_carrier),
        },
    )
    r.expire(f"run:{run_id}", RUN_TTL_S)


def set_status(run_id: str, status: str, **fields: Any) -> None:
    mapping = {"status": status, "updated_at": time.time()}
    for k, v in fields.items():
        mapping[k] = json.dumps(v) if isinstance(v, (dict, list)) else v
    get_redis().hset(f"run:{run_id}", mapping=mapping)


def get_run(run_id: str) -> dict | None:
    raw = get_redis().hgetall(f"run:{run_id}")
    if not raw:
        return None
    out: dict[str, Any] = dict(raw)
    for key in ("bid", "options", "result", "trace_carrier"):
        if key in out:
            try:
                out[key] = json.loads(out[key])
            except (TypeError, ValueError):
                pass
    for key in ("created_at", "updated_at", "completed_at"):
        if key in out:
            out[key] = float(out[key])
    return out


def get_events(run_id: str) -> list[dict]:
    return [json.loads(e) for e in get_redis().lrange(f"run:{run_id}:events", 0, -1)]
