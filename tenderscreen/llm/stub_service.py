"""``llm-stub`` - an OpenAI-compatible fake provider (requirement D6).

Returns fixed (deterministic) responses after a realistic delay, so load
tests measure *our* system instead of a free tier's rate limit. The delay is
drawn from a normal distribution (STUB_DELAY_MS mean, STUB_JITTER_MS sd).

Fault injection for the reliability demo:
    POST /admin/mode {"mode": "ok" | "down" | "slow" | "flaky"}
      down  -> every call returns HTTP 503 (provider outage)
      slow  -> every call sleeps 60 s (forces gateway timeouts)
      flaky -> 30% of calls fail with 503
(or simply ``docker compose stop llm-stub``).
"""
from __future__ import annotations

import asyncio
import os
import random
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from pydantic import BaseModel

from ..common.logging import setup_logging
from .stub_logic import estimate_tokens, respond

logger = setup_logging("llm-stub", os.environ.get("LOG_LEVEL", "INFO"))
DELAY_MS = float(os.environ.get("STUB_DELAY_MS", "800"))
JITTER_MS = float(os.environ.get("STUB_JITTER_MS", "200"))
MODE = {"mode": os.environ.get("STUB_MODE", "ok")}

CALLS = Counter("stub_calls_total", "Stub calls", ["mode", "outcome"])
app = FastAPI(title="LLM stub (OpenAI-compatible)")


class ModeRequest(BaseModel):
    mode: str


@app.post("/v1/chat/completions")
async def chat(body: dict):
    mode = MODE["mode"]
    if mode == "down" or (mode == "flaky" and random.random() < 0.3):
        CALLS.labels(mode, "error").inc()
        return JSONResponse(status_code=503, content={"error": {"message": "stub provider unavailable"}})
    delay = 60.0 if mode == "slow" else max(0.05, random.gauss(DELAY_MS, JITTER_MS) / 1000)
    await asyncio.sleep(delay)
    messages = body.get("messages", [])
    content = respond(messages)
    pt = sum(estimate_tokens(m.get("content", "")) for m in messages)
    ct = estimate_tokens(content)
    CALLS.labels(mode, "ok").inc()
    return {
        "id": f"stub-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", "stub"),
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
    }


@app.post("/admin/mode")
def set_mode(req: ModeRequest):
    if req.mode not in ("ok", "down", "slow", "flaky"):
        return JSONResponse(status_code=422, content={"error": "mode must be ok|down|slow|flaky"})
    MODE["mode"] = req.mode
    logger.warning("stub mode changed to %s", req.mode)
    return MODE


@app.get("/health")
def health():
    return {"status": "ok", "mode": MODE["mode"], "delay_ms": DELAY_MS}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
