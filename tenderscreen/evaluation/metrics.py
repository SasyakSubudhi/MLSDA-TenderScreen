"""Quality metrics (requirement L3) - a deterministic, TruLens-equivalent
implementation. Each metric is computed from the research hand-off (the
*draft*, before verification) so the effect of the verifier is visible, and
also from the final result.

groundedness       fraction of citations whose chunk was retrieved in this run
                   AND whose quoted text occurs verbatim in that chunk
context_relevance  fraction of retrieved passages that are relevant: they
                   carry the tag of a clause the run depends on - the governing
                   tender clause of any of the five conditions, plus the
                   scenario's labelled supporting passages (or, in live
                   scoring, the policy clauses the findings cite)
faithfulness       fraction of findings whose stated status is reproduced by
                   the rule engine when it is given ONLY the cited passages
                   (the claim follows from its citations)
hallucination_rate fraction of findings with any unsupported element: a
                   citation that does not resolve, an evidence id in the text
                   that is not the finding's own, or a number in the text that
                   appears neither in the bid nor in the cited quotes
"""
from __future__ import annotations

import re

from ..agents.conditions import evaluate_all
from ..agents.verifier import _allowed_numbers, _numbers
from ..retrieval.normalize import canon

TENDER_CLAUSES_BY_CONDITION = {
    "C1_TURNOVER": {"ANNEX-II-7", "TENDER-5.9"},
    "C2_EXPERIENCE": {"ANNEX-II-6", "ANNEX-II-8", "TENDER-5.9"},
    "C3_EMD": {"TENDER-5.3", "ANNEX-VIII"},
    "C4_DOCUMENTS": {"TENDER-5.9", "ANNEX-II-1", "ANNEX-II-2", "TENDER-5.10", "ANNEX-II-10", "ANNEX-VII"},
    "C5_REGIONAL_PRESENCE": {"ANNEX-II-4-5"},
}


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def groundedness(handoff: dict) -> float | None:
    ev = handoff.get("evidence", {})
    total = ok = 0
    for f in handoff["draft"]["findings"]:
        for c in f.get("citations", []):
            total += 1
            p = ev.get(c.get("chunk_id"))
            if p is not None and c.get("quote") and canon(c["quote"]) in canon(p["text"]):
                ok += 1
    return _ratio(ok, total) if total else 1.0


def context_relevance(handoff: dict, supporting: set[str] | None = None) -> float | None:
    ev = handoff.get("evidence", {})
    if not ev:
        return 0.0
    # every run screens all five conditions, so their governing tender clauses are always relevant;
    # labelled runs add the scenario's supporting passages, live runs add the policy clauses actually cited
    base = set().union(*TENDER_CLAUSES_BY_CONDITION.values())
    if supporting is not None:
        supporting = base | set(supporting)
    else:
        supporting = base
        for f in handoff["draft"]["findings"]:
            for c in f.get("citations", []):
                p = ev.get(c.get("chunk_id"))
                if p and p["family"] == "policy":
                    supporting.update(p.get("clause_ids") or [p["chunk_id"]])
    rel = sum(1 for p in ev.values() if set(p.get("clause_ids") or []) & supporting)
    return _ratio(rel, len(ev))


def faithfulness(handoff: dict) -> float | None:
    ev = handoff.get("evidence", {})
    draft = handoff["draft"]
    cited_ids = {c["chunk_id"] for f in draft["findings"] for c in f.get("citations", [])}
    cited = [ev[i] for i in cited_ids if i in ev]
    recomputed = {f["condition_id"]: f["status"] for f in evaluate_all(handoff["bid"], handoff["profile"], cited)}
    ok = sum(1 for f in draft["findings"] if recomputed.get(f["condition_id"]) == f["status"])
    return _ratio(ok, len(draft["findings"]))


def hallucination_rate(handoff: dict) -> float | None:
    ev = handoff.get("evidence", {})
    bad = 0
    findings = handoff["draft"]["findings"]
    for f in findings:
        problem = False
        own = {c.get("evidence_id") for c in f.get("citations", [])}
        for c in f.get("citations", []):
            p = ev.get(c.get("chunk_id"))
            if p is None or not c.get("quote") or canon(c["quote"]) not in canon(p["text"]):
                problem = True
        text = f.get("justification", "")
        if any(ref not in own for ref in re.findall(r"\[(E\d+)\]", text)):
            problem = True
        if _numbers(text) - _allowed_numbers(handoff, f):
            problem = True
        bad += problem
    return _ratio(bad, len(findings))


def expected_passage_recall(handoff: dict, supporting: set[str]) -> float | None:
    """Share of the scenario's supporting clauses that were retrieved."""
    if not supporting:
        return None
    got = set()
    for p in handoff.get("evidence", {}).values():
        got.update(p.get("clause_ids") or [])
    return _ratio(len(supporting & got), len(supporting))


def score(handoff: dict, supporting: set[str] | None = None) -> dict:
    return {
        "groundedness": groundedness(handoff),
        "context_relevance": context_relevance(handoff, supporting),
        "faithfulness": faithfulness(handoff),
        "hallucination_rate": hallucination_rate(handoff),
    }
