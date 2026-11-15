"""Batch evaluation from the command line (requirements L2, L3).

    python -m tenderscreen.evaluation.run                     # default prompt version
    python -m tenderscreen.evaluation.run --compare v1 v2     # prompt comparison
    python -m tenderscreen.evaluation.run --set RETRIEVAL_MODE=semantic --label before
    python -m tenderscreen.evaluation.run --llm-url http://llm-gateway:8100   # through the real gateway
    python -m tenderscreen.evaluation.run --demo-fault unsupported_citation --label faulty-model

Runs every scenario in eval/scenarios.json through the same research ->
verifier pipeline as production (in-process; stub model by default), and
reports per scenario and in aggregate:
  route accuracy, condition-status accuracy, expected-passage recall, and the
  four quality metrics (groundedness, context relevance, faithfulness,
  hallucination rate) on the DRAFT (before verification) and on the FINAL
  answer, plus steps, tool calls, LLM calls, tokens and cost.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import statistics
import sys
import time


def _set_path(d: dict, path: str, value) -> None:
    keys = path.split(".")
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    if value is None:
        d.pop(keys[-1], None)
    else:
        d[keys[-1]] = value


def build_bid(scn: dict, examples_dir: str) -> dict:
    with open(os.path.join(examples_dir, scn["base"] + ".json"), encoding="utf-8") as fh:
        bid = json.load(fh)
    bid = copy.deepcopy(bid)
    bid["bid_id"] = f"EVAL-{scn['id']}"
    for path, value in scn.get("overrides", {}).items():
        _set_path(bid, path, value)
    return bid


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 4) if xs else None


def evaluate(prompt_version: str, label: str, llm_url: str | None, scenario_file: str,
             demo_fault: str | None = None, only: list[str] | None = None) -> dict:
    from ..agents.pipeline import Pipeline
    from ..api.contracts import validate_bid
    from ..common.config import get_settings
    from ..common.runstore import MemoryTrace
    from ..llm.client import GatewayClient, InProcessStubClient
    from ..retrieval.store import CorpusIndex
    from .metrics import expected_passage_recall, score

    s = get_settings()
    if not os.path.exists(os.path.join(s.index_dir, "meta.json")):
        from ..retrieval.ingest import run as ingest
        ingest()
    with open(scenario_file, encoding="utf-8") as fh:
        scenarios = json.load(fh)["scenarios"]
    if only:
        scenarios = [x for x in scenarios if x["id"] in set(only)]
    llm = GatewayClient(llm_url, s.llm_call_timeout_s) if llm_url else InProcessStubClient()
    trace = MemoryTrace()
    pipe = Pipeline(CorpusIndex(), llm, trace)
    rows = []
    for scn in scenarios:
        bid = build_bid(scn, s.examples_dir)
        valid, errors = validate_bid(bid)
        if valid is None:
            raise SystemExit(f"scenario {scn['id']} is not a valid bid: {errors}")
        t0 = time.time()
        opts = {"prompt_version": prompt_version, **({"demo_fault": demo_fault} if demo_fault else {})}
        result, handoffs = pipe.run(valid.model_dump(mode="json"), opts,
                                    run_id=f"{label}-{scn['id']}")
        first = handoffs[0]
        support = set(scn["supporting_passages"])
        draft_q = score(first, support)
        final_q = score(handoffs[-1], support) if result.get("verified") else {k: None for k in draft_q}
        statuses = {f["condition_id"]: f["status"] for f in handoffs[-1]["draft"]["findings"]}
        exp = scn.get("expected_statuses", {})
        status_ok = sum(1 for k, v in exp.items() if statuses.get(k) == v)
        events = trace.get(f"{label}-{scn['id']}")
        llm_events = [e for e in events if e["type"] == "llm_call" and e.get("ok") is not None]
        rows.append({
            "id": scn["id"], "title": scn["title"], "expected_route": scn["expected_route"], "route": result["route"],
            "route_correct": result["route"] == scn["expected_route"],
            "status_accuracy": round(status_ok / len(exp), 4) if exp else None,
            "statuses": statuses, "verified": result.get("verified"), "attempts": len(handoffs),
            "expected_passage_recall": expected_passage_recall(first, support),
            "draft": draft_q, "final": final_q,
            "steps": sum(h["stats"]["steps"] for h in handoffs),
            "tool_calls": sum(h["stats"]["tool_calls"] for h in handoffs),
            "llm_calls": len(llm_events),
            "prompt_tokens": sum(int(e.get("prompt_tokens") or 0) for e in llm_events),
            "completion_tokens": sum(int(e.get("completion_tokens") or 0) for e in llm_events),
            "cost_usd": round(sum(float(e.get("cost_usd") or 0) for e in llm_events), 8),
            "cost_by_task": {t: round(sum(float(e.get("cost_usd") or 0) for e in llm_events if e["task"] == t), 8)
                             for t in {e["task"] for e in llm_events}},
            "seconds": round(time.time() - t0, 3),
        })
    agg = {
        "label": label, "prompt_version": prompt_version, "scenarios": len(rows),
        "route_accuracy": _mean([1.0 if r["route_correct"] else 0.0 for r in rows]),
        "status_accuracy": _mean([r["status_accuracy"] for r in rows]),
        "expected_passage_recall": _mean([r["expected_passage_recall"] for r in rows]),
        "verified_rate": _mean([1.0 if r["verified"] else 0.0 for r in rows]),
        "section_4_1_case": next((r["route"] for r in rows if r["id"] == "S02"), None),
    }
    for stage in ("draft", "final"):
        for m in ("groundedness", "context_relevance", "faithfulness", "hallucination_rate"):
            agg[f"{stage}_{m}"] = _mean([r[stage][m] for r in rows])
    for k in ("steps", "tool_calls", "llm_calls", "prompt_tokens", "completion_tokens", "cost_usd"):
        agg[f"mean_{k}"] = _mean([r[k] for r in rows])
    tasks = {}
    for r in rows:
        for t, c in r["cost_by_task"].items():
            tasks[t] = tasks.get(t, 0) + c
    agg["cost_per_100_runs_usd"] = round(sum(r["cost_usd"] for r in rows) / len(rows) * 100, 6)
    agg["cost_by_task_share"] = {t: round(c / max(sum(tasks.values()), 1e-12), 3) for t, c in sorted(tasks.items())}
    agg["largest_cost_driver"] = max(tasks, key=tasks.get) if tasks else None
    return {"aggregate": agg, "rows": rows}


def table(results: list[dict]) -> str:
    keys = ["route_accuracy", "status_accuracy", "expected_passage_recall", "verified_rate",
            "draft_groundedness", "draft_context_relevance", "draft_faithfulness", "draft_hallucination_rate",
            "final_groundedness", "final_faithfulness", "final_hallucination_rate",
            "mean_tool_calls", "mean_llm_calls", "mean_prompt_tokens", "cost_per_100_runs_usd", "section_4_1_case"]
    head = "| metric | " + " | ".join(r["aggregate"]["label"] for r in results) + " |"
    sep = "|---|" + "---|" * len(results)
    lines = [head, sep]
    for k in keys:
        lines.append(f"| {k} | " + " | ".join(str(r["aggregate"].get(k)) for r in results) + " |")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run the fixed evaluation set")
    ap.add_argument("--prompt-version", default=None)
    ap.add_argument("--compare", nargs="+", help="prompt versions to compare on the same set")
    ap.add_argument("--set", action="append", default=[], help="override a setting, e.g. RETRIEVAL_MODE=semantic")
    ap.add_argument("--label", default=None)
    ap.add_argument("--llm-url", default=None, help="use the LLM gateway at this URL instead of the in-process stub")
    ap.add_argument("--scenarios", default="eval/scenarios.json")
    ap.add_argument("--only", nargs="+", help="run only these scenario ids (e.g. for a slow live model)")
    ap.add_argument("--demo-fault", choices=["unsupported_citation"],
                    help="simulate a model that cites an unretrieved passage on every first draft")
    ap.add_argument("--out", default="results")
    args = ap.parse_args(argv)
    for kv in args.set:
        k, v = kv.split("=", 1)
        os.environ[k] = v
    import logging
    logging.disable(logging.WARNING)
    from ..common.config import get_settings
    versions = args.compare or [args.prompt_version or get_settings().prompt_version]
    os.makedirs(args.out, exist_ok=True)
    results = []
    for pv in versions:
        label = args.label if (args.label and len(versions) == 1) else pv
        res = evaluate(pv, label, args.llm_url, args.scenarios, args.demo_fault, args.only)
        results.append(res)
        path = os.path.join(args.out, f"eval_{label}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=1)
        a = res["aggregate"]
        print(f"[{label}] route accuracy {a['route_accuracy']}  4.1 case -> {a['section_4_1_case']}  "
              f"draft groundedness {a['draft_groundedness']}  hallucination {a['draft_hallucination_rate']}  -> {path}")
        for r in res["rows"]:
            mark = "ok " if r["route_correct"] else "XX "
            print(f"   {mark}{r['id']} expected {r['expected_route']:30s} got {r['route']:30s} attempts={r['attempts']}")
    print()
    print(table(results))
    return 0 if all(r["aggregate"]["route_accuracy"] is not None for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
