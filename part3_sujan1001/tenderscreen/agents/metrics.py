"""Prometheus metrics for both agents (requirement A6)."""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

AGENT_TASKS = Counter("agent_tasks_total", "Agent task outcomes", ["agent", "outcome"])
AGENT_STEPS = Histogram("agent_steps_per_run", "Reasoning steps per run", ["agent"], buckets=(1, 2, 4, 6, 8, 10, 12, 16, 20))
AGENT_TOOL_CALLS = Histogram("agent_tool_calls_per_run", "Tool calls per run", ["agent"],
                             buckets=(1, 2, 4, 6, 8, 10, 12, 16, 20, 30))
AGENT_LLM_CALLS = Histogram("agent_llm_calls_per_run", "LLM calls per run", ["agent"], buckets=(0, 1, 2, 3, 4, 6, 8, 12))
AGENT_RUN_SECONDS = Histogram("agent_run_seconds", "Time an agent spends on one run", ["agent"],
                              buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60, 120))
STEP_SECONDS = Histogram("agent_step_seconds", "Latency per agent step", ["agent", "step"],
                         buckets=(0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 16))
TOOL_CALLS = Counter("agent_tool_calls_total", "Tool calls", ["agent", "tool", "outcome"])
RETRIES = Counter("agent_retries_total", "Retries", ["agent", "kind"])
FALLBACKS = Counter("agent_fallbacks_total", "Fallback path used", ["agent", "what"])
RETRIEVAL_CACHE = Counter("retrieval_cache_total", "Retrieval cache lookups", ["result"])

E2E_SECONDS = Histogram("run_e2e_seconds", "Submission to final result", ["route"],
                        buckets=(1, 2, 4, 6, 8, 10, 15, 20, 30, 45, 60, 90, 120, 180))
RUNS_FINAL = Counter("runs_finalized_total", "Final results", ["route", "verified", "degraded"])
VERIFY_CHECKS = Counter("verification_checks_total", "Verifier check outcomes", ["check", "outcome"])
LIVE_QUALITY = Gauge("live_quality_score", "Latest sampled live quality metric", ["metric"])
LIVE_QUALITY_HIST = Histogram("live_quality", "Sampled live quality metric", ["metric"],
                              buckets=(0.0, 0.25, 0.5, 0.75, 0.9, 0.95, 1.0))

INDEX_CHUNKS = Gauge("index_chunks", "Chunks in the loaded index")
INDEX_BUILT = Gauge("index_built_timestamp_seconds", "When the index was last changed")
INDEX_THROUGHPUT = Gauge("ingest_chunks_per_second", "Throughput of the last ingestion run")
INDEX_EMBEDDED = Gauge("ingest_chunks_embedded_last_run", "Chunks embedded by the last ingestion run")
QUEUE_DEPTH = Gauge("queue_depth", "Messages in a stream", ["stream"])
