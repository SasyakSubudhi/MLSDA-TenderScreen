"""The research agent's tools (requirement A1).

Retrieval is a tool the agent *chooses* to call, not a fixed stage:

    search_corpus(query, family, mode, k)   hybrid / semantic / keyword search,
                                            optionally restricted to the tender
                                            or to the policy documents
    lookup_clause(clause_id)                fetch one clause by identifier
    lookup_bid_fields(fields)               read the request's own fields and
                                            classify the bidder (F3)

Every call has a timeout and one bounded retry with backoff, is cached where
safe (retrieval results, keyed by index version), is counted against the
run's tool budget, and is written to the run trace with its arguments,
latency and returned passages.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import time

from ..common.config import get_settings
from ..retrieval.store import CorpusIndex
from . import metrics as M
from .conditions import classify_bidder

TOOL_SPECS = {
    "search_corpus": {
        "description": "Search the indexed corpus. family=tender|policy|all; mode=hybrid|semantic|keyword.",
        "args": {"query": "str", "family": "tender|policy|all", "mode": "hybrid|semantic|keyword", "k": "int 1-8"},
    },
    "lookup_clause": {"description": "Fetch one clause by identifier (e.g. TENDER-3H2, GFR-173).",
                      "args": {"clause_id": "str"}},
    "lookup_bid_fields": {"description": "Read the bid's own fields and classify the bidder.",
                          "args": {"fields": "list[str]"}},
}

_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool")


class ToolError(RuntimeError):
    pass


class Toolbox:
    def __init__(self, index: CorpusIndex, trace, agent: str = "research", redis=None):
        self.index = index
        self.trace = trace
        self.agent = agent
        self.redis = redis
        self.s = get_settings()

    # ------------------------------------------------------------ tools
    def _search_corpus(self, query: str, family: str = "all", mode: str = "hybrid", k: int = 4, **_) -> list[dict]:
        query = str(query)[:300]
        k = max(1, min(int(k), 8))
        mode = mode if mode in ("hybrid", "semantic", "keyword") else "hybrid"
        family = family if family in ("tender", "policy", "all") else "all"
        key = None
        if self.redis is not None and self.s.retrieval_cache_enabled:
            raw = json.dumps([self.index.version, query, family, mode, k])
            key = "ret:" + hashlib.sha256(raw.encode()).hexdigest()
            try:
                hit = self.redis.get(key)
            except Exception:
                hit = None
            if hit:
                M.RETRIEVAL_CACHE.labels("hit").inc()
                return json.loads(hit)
            M.RETRIEVAL_CACHE.labels("miss").inc()
        out = self.index.search(query, family=family, mode=mode, k=k)
        if key:
            try:
                self.redis.set(key, json.dumps(out), ex=self.s.retrieval_cache_ttl_s)
            except Exception:
                pass
        return out

    def _lookup_clause(self, clause_id: str, **_) -> list[dict]:
        p = self.index.lookup(str(clause_id))
        return [p] if p else []

    @staticmethod
    def _lookup_bid_fields(bid: dict, fields=None, **_) -> dict:
        return classify_bidder(bid)

    # ------------------------------------------------------------ dispatcher
    def call(self, run_id: str, name: str, args: dict, bid: dict, step: str):
        """Run a tool with timeout + one retry. Returns (result, info)."""
        if name not in TOOL_SPECS:
            raise ToolError(f"tool not allowlisted: {name}")
        fn = {
            "search_corpus": lambda: self._search_corpus(**args),
            "lookup_clause": lambda: self._lookup_clause(**args),
            "lookup_bid_fields": lambda: self._lookup_bid_fields(bid, **args),
        }[name]
        attempts, last_err, t0 = 0, None, time.time()
        result = None
        for attempt in range(2):
            attempts += 1
            try:
                result = _POOL.submit(fn).result(timeout=self.s.tool_timeout_s)
                last_err = None
                break
            except concurrent.futures.TimeoutError:
                last_err = "timeout"
            except Exception as exc:  # noqa: BLE001 - tool errors are reported, not raised
                last_err = f"{type(exc).__name__}: {exc}"[:200]
            if attempt == 0:
                M.RETRIES.labels(self.agent, "tool").inc()
                time.sleep(0.2)
        latency = (time.time() - t0) * 1000
        ok = last_err is None
        M.TOOL_CALLS.labels(self.agent, name, "ok" if ok else "error").inc()
        M.STEP_SECONDS.labels(self.agent, f"tool:{name}").observe(latency / 1000)
        event = {"agent": self.agent, "step": step, "tool": name, "args": args, "ok": ok, "error": last_err,
                 "attempts": attempts, "latency_ms": round(latency, 1)}
        if ok and isinstance(result, list):
            event["passages"] = [{k: p[k] for k in ("chunk_id", "family", "source", "page", "original_page", "score",
                                                     "retrieval_mode", "clause_ids", "text")} for p in result]
        elif ok:
            event["result"] = {"category": result["category"], "declared": result["declared_category"],
                               "reasons": result["reasons"], "entities": result["entities"]}
        self.trace.event(run_id, "tool_call", **event)
        if not ok:
            raise ToolError(last_err)
        return result, {"attempts": attempts, "latency_ms": latency}
