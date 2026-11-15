"""Research agent (agent 1 of 2) - LangGraph.

    START -> plan --(actions chosen)--> act -> reflect -> plan ...
                 `--(nothing left / limit hit)--> assess -> END

plan     builds the list of *candidate* tool calls from what is still unknown
         and asks the model to choose which to run (validated against an
         allowlist; deterministic fallback if the model is unavailable).
act      executes the chosen tool calls (timeouts, retries, budget).
reflect  reads what came back. For each condition whose governing tender
         clause was found it decides whether another provision might modify
         it (step 2), asks the model for queries derived from the clause it
         has read (step 3), and queues cross-references it noticed
         (e.g. an OM that refers to an earlier OM).
assess   runs the rule engine over the retrieved passages, decides the route,
         and has the model draft the justification with evidence ids.

The output is handed to the verifier agent (a different process) which
checks it against the retrieved passages (step 4) and either accepts it,
sends it back for another attempt with a different strategy, or escalates
(step 5).

Loop bounds (A1): AGENT_MAX_STEPS plan iterations, AGENT_MAX_TOOL_CALLS tool
calls, and the run's wall-clock deadline (RUN_TIMEOUT_S from submission,
queue wait included).
"""
from __future__ import annotations

import json
import re
import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from ..common.config import get_settings
from ..common.logging import agent_name, correlation_id
from ..common.telemetry import tracer
from ..llm.client import LLMUnavailable
from . import metrics as M
from .conditions import (CATEGORY_CONDITION, CONDITION_BY_ID, CONDITIONS, ROUTE_LABELS, decide_route,
                         evaluate_all, evaluate_condition, needs_modifier_search)
from .guardrails import fence_untrusted
from .prompts import render, version_tag
from .tools import ToolError, Toolbox

AGENT = "research"

# condition -> [(anchor regex, clause id for exact lookup, search queries)]
ANCHOR_PLAN: dict[str, list[tuple[str, str, tuple[str, ...]]]] = {
    "C1_TURNOVER": [(CONDITION_BY_ID["C1_TURNOVER"].anchors[0], "ANNEX-II-7",
                     CONDITION_BY_ID["C1_TURNOVER"].tender_queries)],
    "C2_EXPERIENCE": [
        (CONDITION_BY_ID["C2_EXPERIENCE"].anchors[0], "ANNEX-II-6", CONDITION_BY_ID["C2_EXPERIENCE"].tender_queries),
        (CONDITION_BY_ID["C2_EXPERIENCE"].anchors[1], "ANNEX-II-8",
         ("supplied desktop computers to Centrally Funded Technical Institutions in the last five years purchase orders",
          "should have supplied Desktop Computers to Centrally Funded Technical Institutions")),
    ],
    "C3_EMD": [
        (CONDITION_BY_ID["C3_EMD"].anchors[0], "TENDER-5.3", CONDITION_BY_ID["C3_EMD"].tender_queries),
        (CONDITION_BY_ID["C3_EMD"].anchors[1], "ANNEX-VIII",
         ("list of equipment estimated value EMD amount in INR annexure", "Annexure-VIII LIST OF EQUIPMENT EMD (INR)")),
    ],
    "C4_DOCUMENTS": [
        (r"enclose the audited statements", "TENDER-5.9",
         ("enclose audited statements of the financial years certified by a Chartered Accountant",
          "audited statements Chartered Accountant")),
        (r"documentary evidence \(Purchase Order\)", "TENDER-5.9",
         ("documentary evidence purchase order certificate of satisfactory performance of the supplied equipment",
          "documentary evidence (Purchase Order) certificate of satisfactory performance")),
        (r"Registration incorporation certificate", "ANNEX-II-2",
         ("company registered under the Companies Act registration incorporation certificate",
          "Registration incorporation certificate must be submitted")),
        (r"Copy of Tender Document marked", "TENDER-5.10",
         ("copy of tender document marked Original each page signed and stamped",
          "Copy of Tender Document marked Original")),
        (r"Compliance sheet for the technical specification", "ANNEX-II-10",
         ("compliance sheet for the technical specification and OEM brochure",
          "Compliance sheet technical specification OEM Brochure")),
        (r"pages, serially numbered", "ANNEX-VII",
         ("declaration pages serially numbered principal exclusive authorization letter",
          "Annexure-VII DECLARATION pages serially numbered")),
        (r"authorized representative \(provide documentary proof\)", "ANNEX-II-1",
         ("bidder Original Equipment Manufacturer or authorized representative of an OEM documentary proof",
          "Original Equipment Manufacturer (OEM) authorized representative")),
    ],
    "C5_REGIONAL_PRESENCE": [(CONDITION_BY_ID["C5_REGIONAL_PRESENCE"].anchors[0], "ANNEX-II-4-5",
                              CONDITION_BY_ID["C5_REGIONAL_PRESENCE"].tender_queries)],
}

# cross-references the agent follows when it reads them in a policy passage
REFERENCE_PATTERNS = [
    (re.compile(r"O\.?M\.? of even number dated 25th July, 2016", re.I), "DOE-2016-07-REPRINT"),
    (re.compile(r"\bRule 170\b"), "GFR-170"),
    (re.compile(r"\bRule 173\b"), "GFR-173"),
]
GFR2005_PATTERN = re.compile(r"provisions of GFR,? 2005", re.I)

FALLBACK_QUERIES = {
    "C1_TURNOVER": "relaxation of prior turnover criteria for {cat} in public procurement",
    "C2_EXPERIENCE": "relaxation of prior experience criteria for {cat} in public procurement",
    "C3_EMD": "Bid Security Earnest Money exemption for {cat}",
}
CAT_WORDS = {"startup": "Startups", "mse": "Micro and Small Enterprises MSEs"}
# output caps per task: bounded completions are a latency and cost control (L5)
MAX_TOKENS = {"plan": 200, "derive_queries": 300, "draft": 900}


class ResearchState(TypedDict, total=False):
    run_id: str
    bid: dict
    options: dict
    attempt: int
    strategy: str
    prior_failures: list
    created_at: float
    deadline: float
    prompt_version: str
    steps: int
    tool_calls: int
    llm_calls: int
    usage: dict
    profile: dict
    evidence: dict
    anchors: dict            # cond_id -> {anchor: chunk_id | None}
    anchor_tries: dict       # cond_id -> {anchor: int}
    modifier_decided: dict   # cond_id -> bool
    derived: dict            # cond_id -> [{"query","family"}]
    queries_run: list
    pending_refs: list
    refs_done: list
    queued: list
    history: list
    degraded: bool
    incomplete: bool
    stop_reason: str
    draft: dict


def _parse_json(text: str) -> dict | None:
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


class ResearchAgent:
    def __init__(self, index, llm, trace, redis=None):
        self.index = index
        self.llm = llm
        self.trace = trace
        self.tools = Toolbox(index, trace, AGENT, redis)
        self.s = get_settings()
        self.graph = self._build()

    # ================================================================ graph
    def _build(self):
        g = StateGraph(ResearchState)
        g.add_node("plan", self._timed("plan", self.plan))
        g.add_node("act", self._timed("act", self.act))
        g.add_node("reflect", self._timed("reflect", self.reflect))
        g.add_node("assess", self._timed("assess", self.assess))
        g.add_edge(START, "plan")
        g.add_conditional_edges("plan", lambda st: "act" if st.get("queued") else "assess",
                                {"act": "act", "assess": "assess"})
        g.add_edge("act", "reflect")
        g.add_edge("reflect", "plan")
        g.add_edge("assess", END)
        return g.compile()

    def _timed(self, name, fn):
        def wrapper(st):
            t0 = time.time()
            with tracer("research-agent").start_as_current_span(f"research.{name}") as span:
                span.set_attribute("run_id", st["run_id"])
                out = fn(st)
            M.STEP_SECONDS.labels(AGENT, name).observe(time.time() - t0)
            return out
        return wrapper

    # ================================================================ entry
    def run(self, job: dict) -> dict:
        """Run one research attempt; returns the hand-off payload for the verifier."""
        correlation_id.set(job["run_id"])
        agent_name.set(AGENT)
        t0 = time.time()
        pv = job.get("options", {}).get("prompt_version") or self.s.prompt_version
        st: ResearchState = {
            "run_id": job["run_id"], "bid": job["bid"], "options": job.get("options", {}),
            "attempt": int(job.get("attempt", 1)), "strategy": job.get("strategy", "standard"),
            "prior_failures": job.get("prior_failures", []), "created_at": job["created_at"],
            "deadline": job["created_at"] + self.s.run_timeout_s, "prompt_version": pv,
            "steps": 0, "tool_calls": 0, "llm_calls": 0,
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0},
            "profile": {}, "evidence": {}, "anchors": {}, "anchor_tries": {}, "modifier_decided": {},
            "derived": {}, "queries_run": [], "pending_refs": [], "refs_done": [], "queued": [], "history": [],
            "degraded": False, "incomplete": False, "stop_reason": "",
        }
        for cid, plan in ANCHOR_PLAN.items():
            st["anchors"][cid] = {a: None for a, _, _ in plan}
            st["anchor_tries"][cid] = {a: 0 for a, _, _ in plan}
        self.trace.event(job["run_id"], "research_started", agent=AGENT, attempt=st["attempt"],
                         strategy=st["strategy"], prompt_version=version_tag(pv),
                         prior_failures=st["prior_failures"])
        with tracer("research-agent").start_as_current_span("research.run") as span:
            span.set_attribute("run_id", job["run_id"])
            span.set_attribute("attempt", st["attempt"])
            final = self.graph.invoke(st, {"recursion_limit": 4 * self.s.max_steps + 10})
        elapsed = time.time() - t0
        M.AGENT_STEPS.labels(AGENT).observe(final["steps"])
        M.AGENT_TOOL_CALLS.labels(AGENT).observe(final["tool_calls"])
        M.AGENT_LLM_CALLS.labels(AGENT).observe(final["llm_calls"])
        M.AGENT_RUN_SECONDS.labels(AGENT).observe(elapsed)
        M.AGENT_TASKS.labels(AGENT, "incomplete" if final["incomplete"] else
                             "degraded" if final["degraded"] else "ok").inc()
        return {
            "run_id": final["run_id"], "bid": final["bid"], "options": final["options"],
            "created_at": final["created_at"], "attempt": final["attempt"], "strategy": final["strategy"],
            "prompt_version": version_tag(pv), "profile": final["profile"], "evidence": final["evidence"],
            "draft": final["draft"], "degraded": final["degraded"], "incomplete": final["incomplete"],
            "stop_reason": final["stop_reason"],
            "stats": {"steps": final["steps"], "tool_calls": final["tool_calls"], "llm_calls": final["llm_calls"],
                      "research_seconds": round(elapsed, 3), **final["usage"]},
        }

    # ================================================================ llm helper
    def _llm(self, st: ResearchState, task: str, payload: dict, step: str, **params) -> dict | None:
        remaining = st["deadline"] - time.time()
        messages = render(st["prompt_version"], task, payload, **params)
        st["llm_calls"] += 1
        try:
            res = self.llm.complete(task, messages, run_id=st["run_id"], agent=AGENT, step=step,
                                    prompt_version=version_tag(st["prompt_version"]),
                                    max_tokens=MAX_TOKENS.get(task, 800),
                                    timeout_s=min(self.s.llm_call_timeout_s, remaining - 1))
        except LLMUnavailable as exc:
            st["degraded"] = True
            M.FALLBACKS.labels(AGENT, task).inc()
            self.trace.event(st["run_id"], "llm_call", agent=AGENT, step=step, task=task, ok=False,
                             error=exc.reason, prompt_version=version_tag(st["prompt_version"]), fallback=True)
            return None
        st["usage"]["prompt_tokens"] += res.prompt_tokens
        st["usage"]["completion_tokens"] += res.completion_tokens
        st["usage"]["cost_usd"] = round(st["usage"]["cost_usd"] + res.cost_usd, 8)
        parsed = _parse_json(res.content)
        self.trace.event(st["run_id"], "llm_call", agent=AGENT, step=step, task=task, ok=parsed is not None,
                         prompt_version=version_tag(st["prompt_version"]), prompt_tokens=res.prompt_tokens,
                         completion_tokens=res.completion_tokens, cost_usd=res.cost_usd,
                         latency_ms=round(res.latency_ms, 1), cached=res.cached, provider=res.provider,
                         response=res.content[:4000], error=None if parsed is not None else "unparseable_json")
        if parsed is None:
            M.FALLBACKS.labels(AGENT, f"{task}_parse").inc()
        return parsed

    # ================================================================ candidates
    def _candidates(self, st: ResearchState) -> list[dict]:
        if not st["profile"]:
            return [{"id": "A1", "tool": "lookup_bid_fields",
                     "args": {"fields": ["bidder", "financials", "experience", "emd", "documents"]},
                     "purpose": "read the bid's own fields and classify the bidder"}]
        keyword = st["strategy"] != "standard"
        default_mode = "keyword" if keyword else self.s.retrieval_mode
        cands: list[dict] = []
        for cond in CONDITIONS:
            for anchor, clause_id, queries in ANCHOR_PLAN[cond.id]:
                if st["anchors"][cond.id][anchor]:
                    continue
                if clause_id == "ANNEX-II-1" and st["profile"]["entities"]["is_oem"]:
                    continue  # the OEM itself needs no OEM authorisation
                tries = st["anchor_tries"][cond.id][anchor]
                if tries >= 2:
                    continue
                if keyword and tries == 0:  # retry strategy: exact lookup by identifier first
                    cands.append({"tool": "lookup_clause", "args": {"clause_id": clause_id},
                                  "purpose": f"fetch the governing clause of {cond.id} by identifier",
                                  "_anchor": (cond.id, anchor)})
                else:
                    cands.append({"tool": "search_corpus",
                                  "args": {"query": queries[min(tries, len(queries) - 1)], "family": "tender",
                                           "mode": "keyword" if tries else default_mode, "k": self.s.retrieval_k},
                                  "purpose": f"find the tender clause governing {cond.id}", "_anchor": (cond.id, anchor)})
                break  # one open anchor per condition at a time
            for q in st["derived"].get(cond.id, []):
                if q["query"] not in st["queries_run"]:
                    cands.append({"tool": "search_corpus",
                                  "args": {"query": q["query"], "family": q.get("family", "policy"),
                                           "mode": default_mode, "k": self.s.retrieval_k},
                                  "purpose": f"look for a provision that modifies {cond.id} for this bidder category"})
        declared = st["profile"]["declared_category"]
        if declared in ("consortium", "foreign") and "category" not in st["refs_done"]:
            q = ("each tenderer shall submit only one bid joint venture consortium" if declared == "consortium"
                 else "bidder should be a company registered under the Companies Act partnership proprietorship")
            cands.append({"tool": "search_corpus", "args": {"query": q, "family": "all", "mode": default_mode,
                                                            "k": self.s.retrieval_k},
                          "purpose": f"find rules on {declared} bidders", "_ref": "category"})
        for ref in st["pending_refs"]:
            if ref in st["refs_done"]:
                continue
            if ref.startswith("query:"):
                cands.append({"tool": "search_corpus", "args": {"query": ref[6:], "family": "policy",
                                                                "mode": "keyword", "k": self.s.retrieval_k},
                              "purpose": "follow a cross-reference read in a policy passage", "_ref": ref})
            else:
                cands.append({"tool": "lookup_clause", "args": {"clause_id": ref},
                              "purpose": "follow a cross-reference read in a policy passage", "_ref": ref})
        seen, uniq = set(), []
        for c in cands:
            key = json.dumps([c["tool"], c["args"]], sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            c["id"] = f"A{len(uniq) + 1}"
            uniq.append(c)
        return uniq

    # ================================================================ nodes
    def plan(self, st: ResearchState) -> dict:
        now = time.time()
        if st["steps"] >= self.s.max_steps or st["tool_calls"] >= self.s.max_tool_calls or now >= st["deadline"] - 2:
            reason = ("step limit" if st["steps"] >= self.s.max_steps else
                      "tool-call limit" if st["tool_calls"] >= self.s.max_tool_calls else "wall-clock deadline")
            open_left = self._candidates(st)
            incomplete = bool(open_left)
            self.trace.event(st["run_id"], "loop_limit", agent=AGENT, reason=reason, open_actions=len(open_left))
            return {"queued": [], "incomplete": incomplete, "stop_reason": reason if incomplete else "done"}
        cands = self._candidates(st)
        if not cands:
            return {"queued": [], "stop_reason": "all questions answered"}
        st["steps"] += 1
        budget_left = self.s.max_tool_calls - st["tool_calls"]
        max_actions = max(1, min(3, budget_left))
        source, reason = "single", "only one candidate"
        chosen = cands[:1]
        if len(cands) > 1:
            payload = {
                "bidder_category": st["profile"].get("declared_category"),
                "open_conditions": [c.id for c in CONDITIONS if not all(st["anchors"][c.id].values())],
                "max_actions": max_actions,
                "candidates": [{"id": c["id"], "tool": c["tool"], "args": c["args"], "purpose": c["purpose"]}
                               for c in cands],
            }
            out = self._llm(st, "plan", payload, f"plan-{st['steps']}", max_actions=max_actions)
            ids = out.get("actions") if isinstance(out, dict) else None
            valid = [c for i in (ids or []) for c in cands if isinstance(i, str) and c["id"] == i]
            if valid:
                chosen, source, reason = valid[:max_actions], "llm", str(out.get("reason", ""))[:300]
            else:
                chosen, source, reason = cands[:max_actions], "fallback", "model unavailable or invalid choice"
                M.FALLBACKS.labels(AGENT, "plan").inc()
        self.trace.event(st["run_id"], "plan_decision", agent=AGENT, step=st["steps"], decision_source=source,
                         reason=reason, candidates=[{k: c[k] for k in ("id", "tool", "args", "purpose")} for c in cands],
                         chosen=[c["id"] for c in chosen])
        return {"queued": chosen, "steps": st["steps"], "llm_calls": st["llm_calls"], "usage": st["usage"],
                "degraded": st["degraded"]}

    def act(self, st: ResearchState) -> dict:
        history = list(st["history"])
        for action in st["queued"]:
            if st["tool_calls"] >= self.s.max_tool_calls or time.time() >= st["deadline"] - 2:
                break
            st["tool_calls"] += 1
            if "_anchor" in action:
                cid, anchor = action["_anchor"]
                st["anchor_tries"][cid][anchor] += 1
            if action["tool"] == "search_corpus":
                st["queries_run"].append(action["args"]["query"])
            if "_ref" in action:
                st["refs_done"].append(action["_ref"])
            try:
                result, _ = self.tools.call(st["run_id"], action["tool"], action["args"], st["bid"], f"act-{st['steps']}")
            except ToolError as exc:
                history.append({"action": action["id"], "tool": action["tool"], "ok": False, "error": str(exc)})
                continue
            if action["tool"] == "lookup_bid_fields":
                st["profile"] = result
            else:
                for p in result:
                    st["evidence"].setdefault(p["chunk_id"], p)
            history.append({"action": action["id"], "tool": action["tool"], "ok": True,
                            "returned": [p["chunk_id"] for p in result] if isinstance(result, list) else "profile"})
        return {"queued": [], "history": history, "tool_calls": st["tool_calls"], "profile": st["profile"],
                "evidence": st["evidence"], "anchor_tries": st["anchor_tries"], "queries_run": st["queries_run"],
                "refs_done": st["refs_done"]}

    def reflect(self, st: ResearchState) -> dict:
        passages = list(st["evidence"].values())
        tender = [p for p in passages if p["family"] == "tender"]
        # which anchors are now covered by retrieved tender text?
        for cid, anchors in st["anchors"].items():
            for anchor in anchors:
                if anchors[anchor]:
                    continue
                hit = next((p for p in tender if re.search(anchor, p["text"], re.I)), None)
                if hit:
                    anchors[anchor] = hit["chunk_id"]
        profile = st["profile"]
        need = []
        if profile:
            for cond in CONDITIONS:
                if st["modifier_decided"].get(cond.id) or not cond.relaxable:
                    continue
                if not all(st["anchors"][cond.id].values()):
                    continue  # read every governing clause of the condition before judging it
                raw = evaluate_condition(cond, st["bid"], profile, tender)
                st["modifier_decided"][cond.id] = True
                if needs_modifier_search(cond, profile, raw["status"], st["bid"]):
                    gov = st["evidence"][st["anchors"][cond.id][ANCHOR_PLAN[cond.id][0][0]]]
                    need.append({"condition_id": cond.id, "condition_title": cond.title,
                                 "bidder_category": profile["declared_category"],
                                 "tender_clause_text": gov["text"][:700], "status_on_tender_alone": raw["status"]})
        derived_event = None
        if need:
            out = self._llm(st, "derive_queries", {"conditions": need}, f"reflect-{st['steps']}")
            qmap = out.get("queries") if isinstance(out, dict) else None
            for item in need:
                cid = item["condition_id"]
                qs = qmap.get(cid) if isinstance(qmap, dict) else None
                clean = []
                for q in qs or []:
                    if isinstance(q, dict) and isinstance(q.get("query"), str) and q["query"].strip():
                        fam = q.get("family") if q.get("family") in ("tender", "policy", "all") else "policy"
                        clean.append({"query": q["query"].strip()[:200], "family": fam})
                # Safeguard (fix for live-model miss S11): the model's queries decide what ELSE to look
                # for, but a fixed category-specific policy search always runs too, so a relaxation the
                # model's wording happens not to reach cannot be silently missed.
                cat = CAT_WORDS.get(item["bidder_category"], "Startups and Micro and Small Enterprises")
                template = {"query": FALLBACK_QUERIES[cid].format(cat=cat), "family": "policy"}
                clean = clean[:2]
                model_failed = not clean and (out is None or qmap is None)
                if (self.s.policy_safeguard or model_failed) and template["query"] not in [q["query"] for q in clean]:
                    clean.append({**template, ("fallback" if model_failed else "safeguard"): True})
                st["derived"][cid] = clean
            derived_event = {cid: st["derived"].get(cid) for cid in (i["condition_id"] for i in need)}
        # cross-references noticed in policy passages
        new_refs = []
        if profile and profile.get("declared_category") in ("startup", "mse"):
            for p in passages:
                if p["family"] != "policy":
                    continue
                for rx, clause in REFERENCE_PATTERNS:
                    if rx.search(p["text"]) and clause not in st["evidence"]:
                        new_refs.append(clause)
                if GFR2005_PATTERN.search(p["text"]) and profile["declared_category"] == "startup":
                    new_refs.append("query:General Financial Rules prior turnover and prior experience may be "
                                    "relaxed for Startups")
        new_refs = [r for r in dict.fromkeys(new_refs) if r not in st["pending_refs"]]
        st["pending_refs"].extend(new_refs)
        if derived_event or new_refs:
            self.trace.event(st["run_id"], "reflect", agent=AGENT, step=st["steps"], derived_queries=derived_event,
                             cross_references=new_refs)
        return {"anchors": st["anchors"], "modifier_decided": st["modifier_decided"], "derived": st["derived"],
                "pending_refs": st["pending_refs"], "llm_calls": st["llm_calls"], "usage": st["usage"],
                "degraded": st["degraded"]}

    def assess(self, st: ResearchState) -> dict:
        if not st["profile"]:  # could not even read the bid - should never happen
            st["profile"] = Toolbox._lookup_bid_fields(st["bid"])
        passages = list(st["evidence"].values())
        findings = evaluate_all(st["bid"], st["profile"], passages)
        route = decide_route(findings, st["profile"], st["incomplete"], st["degraded"])

        # number evidence E1..En across the run
        eid, ev_index = {}, {}
        for f in findings:
            for e in f["evidence"]:
                key = (e["chunk_id"], e["quote"])
                if key not in eid:
                    eid[key] = f"E{len(eid) + 1}"
                    ev_index[eid[key]] = e
                e["evidence_id"] = eid[key]
        payload = {
            "route_label": route["route_label"],
            "bidder": {"category": st["profile"]["category"], "name": fence_untrusted(st["bid"]["bidder"]["legal_name"], 200)},
            "bidder_remarks": fence_untrusted(st["bid"].get("remarks")),
            "findings": [{"condition_id": f["condition_id"], "title": f["title"], "status": f["status"],
                          "basis": f["basis"], "values": f["values"],
                          "evidence": [{"id": e["evidence_id"], "role": e["role"],
                                        "source": st["evidence"][e["chunk_id"]]["source"],
                                        "page": st["evidence"][e["chunk_id"]]["original_page"], "quote": e["quote"]}
                                       for e in f["evidence"]]} for f in findings],
        }
        drafted = self._llm(st, "draft", payload, "assess") if not st["degraded"] else None
        if st["degraded"]:  # degraded: route must escalate, recompute with the flag
            route = decide_route(findings, st["profile"], st["incomplete"], True)
        by_cond = {}
        if isinstance(drafted, dict) and isinstance(drafted.get("findings"), list):
            by_cond = {d.get("condition_id"): d for d in drafted["findings"] if isinstance(d, dict)}
        draft_source = "llm" if by_cond else ("fallback_degraded" if st["degraded"] else "fallback_template")
        out_findings = []
        for f in findings:
            d = by_cond.get(f["condition_id"], {})
            just = d.get("justification") if isinstance(d.get("justification"), str) else None
            if not just:
                refs = " ".join(f"[{e['evidence_id']}]" for e in f["evidence"])
                just = (f["basis"] + (" " + refs if refs else "")).strip()
            llm_ids = [i for i in d.get("citations", []) if isinstance(i, str)] if d else []
            cited = [dict(e) for e in f["evidence"]]
            # an id chosen by the model that is not this finding's evidence is kept so the verifier can flag it
            for i in llm_ids:
                if i not in {e["evidence_id"] for e in cited}:
                    cited.append({"evidence_id": i, "chunk_id": ev_index.get(i, {}).get("chunk_id", "unknown"),
                                  "quote": ev_index.get(i, {}).get("quote", ""), "role": "model_added"})
            out_findings.append({**{k: f[k] for k in ("condition_id", "title", "status", "basis", "values",
                                                       "tender_clause")},
                                 "justification": just[:1500], "citations": cited})
        summary = drafted.get("summary") if isinstance(drafted, dict) and isinstance(drafted.get("summary"), str) else (
            f"Screening finding: {route['route_label']}. The Tender Committee decides.")

        # demo of self-verification (requirement A3 / demo item 6): simulate a model that cites a passage
        # nobody retrieved, on the first attempt only
        if st["options"].get("demo_fault") == "unsupported_citation" and st["attempt"] == 1:
            target = next((f for f in out_findings if f["condition_id"] == "C1_TURNOVER"), out_findings[-1])
            target["citations"].append({"evidence_id": "E99", "chunk_id": "gfr_2017:p99:w0", "role": "model_added",
                                        "quote": "Startups are exempt from every eligibility criterion in all tenders."})
            target["justification"] += " Startups are exempt from every eligibility criterion in all tenders [E99]."

        draft = {"route": route["route"], "route_label": ROUTE_LABELS[route["route"]],
                 "basis_conditions": route["basis_conditions"], "escalation_reasons": route["escalation_reasons"],
                 "summary": summary[:800], "findings": out_findings, "draft_source": draft_source,
                 "bidder_category": st["profile"]["category"], "declared_category": st["profile"]["declared_category"],
                 "category_reasons": st["profile"]["reasons"], "entities": st["profile"]["entities"]}
        self.trace.event(st["run_id"], "assessment", agent=AGENT, route=route["route"], draft_source=draft_source,
                         statuses={f["condition_id"]: f["status"] for f in out_findings},
                         degraded=st["degraded"], incomplete=st["incomplete"], stop_reason=st["stop_reason"])
        return {"draft": draft, "llm_calls": st["llm_calls"], "usage": st["usage"], "degraded": st["degraded"],
                "profile": st["profile"]}


__all__ = ["ResearchAgent", "ANCHOR_PLAN", "CATEGORY_CONDITION"]
