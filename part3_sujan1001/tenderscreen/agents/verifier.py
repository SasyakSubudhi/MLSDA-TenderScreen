"""Verifier agent (agent 2 of 2) - LangGraph.

Runs in its own container. Receives the research agent's draft through the
``stream:verify`` hand-off and checks it against the passages that were
actually retrieved in this run, *before* anything is returned (A3):

  V1 structure          a finding for every condition; draft well formed
  V2 citations_resolve  every cited chunk was retrieved in this run and the
                        quoted text occurs verbatim in that chunk
  V3 required_support   every condition cites its tender clause; a CONFLICT
                        cites both the tender clause and the policy rule
  V4 status_entailed    re-running the rule engine on ONLY the cited passages
                        gives the same status (the citations really support it)
  V5 route_consistent   the route follows from the statuses and is one of
                        the four allowed routes (output filter)
  V6 text_grounded      justification text cites only its own evidence ids
                        and contains no number absent from bid or sources
  V7 output_policy      no unauthorised determination / toxic wording
                        (sanitised, recorded)

decide: accept -> final result | retry (different strategy) -> back to the
research agent | escalate (unverified answers are never returned).
"""
from __future__ import annotations

import random
import re
import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from ..common.config import get_settings
from ..common.logging import agent_name, correlation_id
from ..common.telemetry import tracer
from ..retrieval.normalize import canon
from . import metrics as M
from .conditions import (CATEGORY_CONDITION, CONDITIONS, ROUTE_LABELS, ROUTES, decide_route, evaluate_all,
                         inr)
from .guardrails import filter_output

AGENT = "verifier"
ALL_CONDITION_IDS = [CATEGORY_CONDITION] + [c.id for c in CONDITIONS]
DISCLAIMER = ("This is a screening finding with supporting evidence, not a decision. "
              "The Tender Committee decides whether to qualify or reject the bid.")


class VerifyState(TypedDict, total=False):
    job: dict
    checks: list
    sanitized: dict
    decision: str
    result: dict


def _numbers(text: str) -> set[str]:
    text = re.sub(r"\[E\d+\]|\bE\d+\b|\bC\d_[A-Z_]+\b", " ", text)
    return {m.replace(",", "") for m in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}


def _allowed_numbers(job: dict, finding: dict) -> set[str]:
    allowed: set[str] = set(str(i) for i in range(10))
    sources = [finding.get("basis", ""), str(finding.get("values", {}))]
    sources += [c.get("quote", "") for c in finding.get("citations", [])]
    sources += [str(v) for v in job["profile"].get("entities", {}).values()]
    for s in sources:
        for m in re.findall(r"\d[\d,]*(?:\.\d+)?", s):
            allowed.add(m.replace(",", ""))
            allowed.update(p for p in m.split(",") if p)
            try:
                f = float(m.replace(",", ""))
                allowed.add(str(int(f)))
                allowed.add(inr(f).replace(",", ""))
            except ValueError:
                pass
    return allowed


class VerifierAgent:
    def __init__(self, trace):
        self.trace = trace
        self.s = get_settings()
        self.graph = self._build()

    def _build(self):
        g = StateGraph(VerifyState)
        for name in ("structure", "citations", "support", "text", "decide"):
            g.add_node(name, self._timed(name, getattr(self, f"n_{name}")))
        g.add_edge(START, "structure")
        g.add_edge("structure", "citations")
        g.add_edge("citations", "support")
        g.add_edge("support", "text")
        g.add_edge("text", "decide")
        g.add_edge("decide", END)
        return g.compile()

    def _timed(self, name, fn):
        def wrapper(st):
            t0 = time.time()
            with tracer("verifier-agent").start_as_current_span(f"verify.{name}") as span:
                span.set_attribute("run_id", st["job"]["run_id"])
                out = fn(st)
            M.STEP_SECONDS.labels(AGENT, name).observe(time.time() - t0)
            return out
        return wrapper

    @staticmethod
    def _check(st, name: str, ok: bool, details: list | None = None) -> None:
        st["checks"].append({"check": name, "passed": bool(ok), "details": (details or [])[:20]})
        M.VERIFY_CHECKS.labels(name, "pass" if ok else "fail").inc()

    # ------------------------------------------------------------- nodes
    def n_structure(self, st):
        draft = st["job"].get("draft") or {}
        ids = [f.get("condition_id") for f in draft.get("findings", [])]
        missing = [c for c in ALL_CONDITION_IDS if c not in ids]
        self._check(st, "V1_structure", bool(draft) and not missing, [f"missing finding {m}" for m in missing])
        return {"checks": st["checks"]}

    def n_citations(self, st):
        job = st["job"]
        evidence = job.get("evidence", {})
        problems = []
        for f in job["draft"].get("findings", []):
            for c in f.get("citations", []):
                p = evidence.get(c.get("chunk_id"))
                if p is None:
                    problems.append(f"{f['condition_id']}: {c.get('evidence_id')} cites {c.get('chunk_id')} which was "
                                    "not retrieved in this run")
                elif not c.get("quote") or canon(c["quote"]) not in canon(p["text"]):
                    problems.append(f"{f['condition_id']}: quote of {c.get('evidence_id')} not found in {c['chunk_id']}")
        self._check(st, "V2_citations_resolve", not problems, problems)
        return {"checks": st["checks"]}

    def n_support(self, st):
        job = st["job"]
        evidence = job.get("evidence", {})
        draft = job["draft"]
        problems = []
        for f in draft.get("findings", []):
            fams = {evidence[c["chunk_id"]]["family"] for c in f.get("citations", []) if c.get("chunk_id") in evidence}
            if f["condition_id"] != CATEGORY_CONDITION and f["status"] != "EVIDENCE_MISSING" and "tender" not in fams:
                problems.append(f"{f['condition_id']}: no tender clause cited")
            if f["status"] == "CONFLICT" and "policy" not in fams:
                problems.append(f"{f['condition_id']}: CONFLICT without a cited policy provision")
        self._check(st, "V3_required_support", not problems, problems)

        # V4 - recompute every status from the bid + ONLY the passages the draft cites
        cited_ids = {c["chunk_id"] for f in draft.get("findings", []) for c in f.get("citations", [])}
        cited = [evidence[i] for i in cited_ids if i in evidence]
        recomputed = {f["condition_id"]: f["status"] for f in evaluate_all(job["bid"], job["profile"], cited)}
        mism = [f"{f['condition_id']}: draft {f['status']} but cited evidence supports {recomputed.get(f['condition_id'])}"
                for f in draft.get("findings", []) if recomputed.get(f["condition_id"]) != f["status"]]
        self._check(st, "V4_status_entailed", not mism, mism)

        expected = decide_route([{"condition_id": f["condition_id"], "status": f["status"]} for f in draft["findings"]],
                                job["profile"], job.get("incomplete", False), job.get("degraded", False))["route"]
        route_ok = draft.get("route") in ROUTES and draft.get("route") == expected
        self._check(st, "V5_route_consistent", route_ok,
                    [] if route_ok else [f"route {draft.get('route')} but statuses imply {expected}"])
        return {"checks": st["checks"]}

    def n_text(self, st):
        job = st["job"]
        problems, flags, sanitized = [], [], {}
        for f in job["draft"].get("findings", []):
            text = f.get("justification", "")
            own = {c.get("evidence_id") for c in f.get("citations", [])}
            for ref in re.findall(r"\[(E\d+)\]", text):
                if ref not in own:
                    problems.append(f"{f['condition_id']}: text cites {ref} which is not its evidence")
            stray = sorted(_numbers(text) - _allowed_numbers(job, f))
            if stray:
                problems.append(f"{f['condition_id']}: number(s) {', '.join(stray[:5])} not in bid or cited sources")
            clean, fl = filter_output(text)
            if fl:
                flags += [f"{f['condition_id']}: {x}" for x in fl]
            sanitized[f["condition_id"]] = clean
        summary, fl = filter_output(job["draft"].get("summary", ""))
        flags += [f"summary: {x}" for x in fl]
        sanitized["__summary__"] = summary
        self._check(st, "V6_text_grounded", not problems, problems)
        st["checks"].append({"check": "V7_output_policy", "passed": True, "details": flags or ["clean"],
                             "sanitised": bool(flags)})
        M.VERIFY_CHECKS.labels("V7_output_policy", "sanitised" if flags else "pass").inc()
        return {"checks": st["checks"], "sanitized": sanitized}

    def n_decide(self, st):
        job = st["job"]
        failed = [c for c in st["checks"] if not c["passed"]]
        expired = time.time() > job["created_at"] + self.s.run_timeout_s
        if not failed:
            decision = "accept"
        elif job["attempt"] < self.s.max_verification_attempts and not expired:
            decision = "retry"
        else:
            decision = "escalate"
        return {"decision": decision, "result": self._result(job, st, decision, failed, expired)}

    # ------------------------------------------------------------- result
    def _result(self, job, st, decision, failed, expired) -> dict:
        draft = job["draft"]
        evidence = job.get("evidence", {})
        verification = {"passed": decision == "accept", "decision": decision, "attempt": job["attempt"],
                        "checks": st["checks"]}
        base = {"run_id": job["run_id"], "prompt_version": job["prompt_version"], "bid_id": job["bid"].get("bid_id"),
                "bidder_name": job["bid"]["bidder"]["legal_name"], "bidder_category": draft.get("bidder_category"),
                "declared_category": draft.get("declared_category"), "category_reasons": draft.get("category_reasons", []),
                "degraded": job.get("degraded", False), "incomplete": job.get("incomplete", False),
                "verification": verification, "disclaimer": DISCLAIMER, "stats": job.get("stats", {})}
        if decision == "retry":
            return base
        if decision == "escalate":
            reasons = ["self-verification failed after the allowed attempts" if not expired else
                       "the run exceeded its deadline before an answer could be verified"]
            reasons += [d for c in failed for d in c["details"]][:8]
            return {**base, "route": "escalate_to_human", "route_label": ROUTE_LABELS["escalate_to_human"],
                    "verified": False, "summary": "No verified answer could be produced; the case is escalated "
                                                  "with the passages that were retrieved.",
                    "findings": [], "withheld": "findings withheld because they could not be verified",
                    "escalation": {"to": "Tender Committee (human review)", "reasons": reasons,
                                   "retrieved_passages": [self._cite(evidence, cid, None, "retrieved")
                                                          for cid in list(evidence)[:8]]}}
        findings = []
        for f in draft["findings"]:
            findings.append({
                "condition_id": f["condition_id"], "title": f["title"], "status": f["status"],
                "justification": st["sanitized"].get(f["condition_id"], f["justification"]),
                "values": f.get("values", {}),
                "citations": [self._cite(evidence, c["chunk_id"], c["quote"], c.get("role"), c.get("evidence_id"))
                              for c in f["citations"] if c.get("chunk_id") in evidence],
            })
        escalation = None
        if draft["route"] == "escalate_to_human":
            cites = [c for f in findings if f["status"] in ("CONFLICT", "ESCALATE", "EVIDENCE_MISSING") for c in f["citations"]]
            steps = []
            conflicts = {f["condition_id"] for f in findings if f["status"] == "CONFLICT"}
            if conflicts & {"C1_TURNOVER", "C2_EXPERIENCE"}:
                steps.append("Committee to decide whether the cited central relaxation of prior turnover / prior "
                             "experience applies to this tender; GFR Rule 173(i) ties it to 'suitable provisions in "
                             "the bidding document' and to meeting quality and technical specifications.")
            if "C3_EMD" in conflicts:
                steps.append("Committee to decide whether the MSE exemption from bid security in GFR Rule 170(i) "
                             "applies, given that the tender asks every bidder for EMD and rejects bids without it.")
            if job.get("degraded"):
                steps.append("Re-run the screening when the model provider is available, or review the rule-based "
                             "findings below manually.")
            if not draft["bidder_category"] in ("large_enterprise", "mse", "startup"):
                steps.append("Confirm the bidder's category and how the eligibility criteria apply to it.")
            escalation = {"to": "Tender Committee (human review)", "reasons": draft["escalation_reasons"],
                          "citations": cites, "next_steps": steps or ["Committee review of the cited provisions."]}
        return {**base, "route": draft["route"], "route_label": ROUTE_LABELS[draft["route"]], "verified": True,
                "summary": st["sanitized"].get("__summary__", draft.get("summary", "")),
                "basis_conditions": draft["basis_conditions"], "findings": findings, "escalation": escalation,
                "entities": draft.get("entities", {}),
                "provisional": bool(job.get("degraded")),
                "draft_source": draft.get("draft_source")}

    @staticmethod
    def _cite(evidence, chunk_id, quote, role, evidence_id=None) -> dict:
        p = evidence[chunk_id]
        return {"evidence_id": evidence_id, "chunk_id": chunk_id, "role": role, "source": p["source"],
                "title": p["title"], "page": p["page"], "original_page": p["original_page"], "url": p["url"],
                "quote": quote if quote else p["text"][:300]}

    # ------------------------------------------------------------- entry
    def verify(self, job: dict) -> tuple[str, dict]:
        correlation_id.set(job["run_id"])
        agent_name.set(AGENT)
        t0 = time.time()
        with tracer("verifier-agent").start_as_current_span("verify.run") as span:
            span.set_attribute("run_id", job["run_id"])
            out = self.graph.invoke({"job": job, "checks": [], "sanitized": {}})
        decision, result = out["decision"], out["result"]
        self.trace.event(job["run_id"], "verification", agent=AGENT, attempt=job["attempt"], decision=decision,
                         checks=out["checks"], latency_ms=round((time.time() - t0) * 1000, 1))
        M.AGENT_RUN_SECONDS.labels(AGENT).observe(time.time() - t0)
        M.AGENT_STEPS.labels(AGENT).observe(5)
        M.AGENT_TOOL_CALLS.labels(AGENT).observe(0)
        M.AGENT_TASKS.labels(AGENT, decision).inc()
        if decision == "retry":
            M.RETRIES.labels(AGENT, "verification").inc()
        return decision, result

    def failed_checks(self, result: dict) -> list[str]:
        return [c["check"] for c in result["verification"]["checks"] if not c["passed"]]

    @staticmethod
    def sample(rate: float) -> bool:
        return random.random() < rate
