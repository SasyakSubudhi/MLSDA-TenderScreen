"""In-process orchestration with exactly the same hand-off semantics as the
containers (research -> verifier -> retry/accept/escalate). Used by the
evaluation batch (no Docker needed) and by the unit tests. The production
path is ``worker.py``, where the hand-off goes through Redis streams.
"""
from __future__ import annotations

import time
import uuid

from ..common.config import get_settings
from .research import ResearchAgent
from .verifier import VerifierAgent

RETRY_STRATEGY = "keyword_lookup"


class Pipeline:
    def __init__(self, index, llm, trace):
        self.research = ResearchAgent(index, llm, trace)
        self.verifier = VerifierAgent(trace)
        self.trace = trace
        self.s = get_settings()

    def run(self, bid: dict, options: dict | None = None, run_id: str | None = None) -> tuple[dict, list[dict]]:
        """Returns (final result, list of research hand-offs - one per attempt)."""
        run_id = run_id or uuid.uuid4().hex[:16]
        created = time.time()
        job = {"run_id": run_id, "bid": bid, "options": options or {}, "attempt": 1, "strategy": "standard",
               "created_at": created}
        handoffs = []
        while True:
            handoff = self.research.run(job)
            handoffs.append(handoff)
            self.trace.event(run_id, "handoff", agent="research", to="verifier", attempt=job["attempt"])
            decision, result = self.verifier.verify(handoff)
            if decision != "retry":
                result["e2e_seconds"] = round(time.time() - created, 3)
                return result, handoffs
            failed = self.verifier.failed_checks(result)
            self.trace.event(run_id, "retry", agent="verifier", next_attempt=job["attempt"] + 1,
                             strategy=RETRY_STRATEGY, failed_checks=failed)
            job = {**job, "attempt": job["attempt"] + 1, "strategy": RETRY_STRATEGY, "prior_failures": failed}
