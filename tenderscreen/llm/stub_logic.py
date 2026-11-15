"""Deterministic stand-in for a chat model.

Used (a) by the ``llm-stub`` container that replaces the provider during load
tests (requirement D6) and default demos, and (b) in-process by the
evaluation batch and unit tests.

It behaves like an instruction-following model with fixed responses: it
reads the ``TASK:`` line of the system prompt and the JSON between
``<<INPUT>>`` and ``<<END>>`` and answers in the JSON shape the prompt asks
for. It never sees the corpus, so it cannot invent evidence on its own.
"""
from __future__ import annotations

import json
import math
import re

_TOPIC_WORDS = {
    "C1_TURNOVER": "prior turnover",
    "C2_EXPERIENCE": "prior experience",
    "C3_EMD": "earnest money deposit bid security",
}
_CAT_WORDS = {"startup": "Startups", "mse": "Micro and Small Enterprises (MSEs)"}


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


def _input(user: str) -> dict:
    m = re.search(r"<<INPUT>>\s*(.*?)\s*<<END>>", user, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


def respond(messages: list[dict]) -> str:
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    task_m = re.search(r"TASK:\s*([a-z_]+)", system)
    task = task_m.group(1) if task_m else ""
    data = _input(user)

    if task == "plan":
        cands = data.get("candidates", [])
        n = int(data.get("max_actions", 3))
        return json.dumps({"actions": [c["id"] for c in cands[:n]],
                           "reason": "Gather the governing clause for each open condition first, then check for "
                                     "provisions that modify it."})

    if task == "derive_queries":
        scope_m = re.search(r"QUERY_SCOPE:\s*([a-z_]+)", system)
        scope = scope_m.group(1) if scope_m else "tender_only"
        out = {}
        for item in data.get("conditions", []):
            cid = item["condition_id"]
            if scope == "tender_and_central_rules":
                topic = _TOPIC_WORDS.get(cid, item.get("condition_title", ""))
                cat = _CAT_WORDS.get(item.get("bidder_category", ""), "this category of bidder")
                out[cid] = [
                    {"query": f"relaxation of {topic} criteria for {cat} in public procurement", "family": "policy"},
                    {"query": f"General Financial Rules {topic} relaxed exempted for {cat}", "family": "policy"},
                ]
            else:  # naive single-pass prompt: restate the condition, search the tender only
                out[cid] = [{"query": item.get("condition_title", cid), "family": "tender"}]
        return json.dumps({"queries": out})

    if task == "draft":
        findings = []
        for f in data.get("findings", []):
            ids = [e["id"] for e in f.get("evidence", [])]
            refs = " ".join(f"[{i}]" for i in ids)
            findings.append({
                "condition_id": f["condition_id"],
                "justification": (f["basis"] + (" " + refs if refs else "")).strip(),
                "citations": ids,
            })
        return json.dumps({"summary": f"Screening finding: {data.get('route_label', '')}. "
                                      "This is a recommendation for the Tender Committee, which decides.",
                           "findings": findings})

    return json.dumps({"text": "ok"})
