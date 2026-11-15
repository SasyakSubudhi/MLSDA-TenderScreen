"""Evaluation with TruLens (requirement L3).

Every scenario of eval/scenarios.json is run through the production pipeline
wrapped as a TruLens ``TruBasicApp``; TruLens records each run (input,
output, latency) in its database and evaluates four TruLens ``Feedback``
functions on it:

    groundedness       (higher is better)
    context_relevance  (higher is better)
    faithfulness       (higher is better)
    hallucination_rate (lower is better)

The feedback implementations are deterministic (``metrics.py``): they check
citations against the passages the run actually retrieved instead of asking a
second LLM to judge the first, so the scores are exact and repeatable. They
are scored on the research agent's draft - before the verifier - so the
verifier's effect shows up in the numbers.

    python -m tenderscreen.evaluation.trulens_eval --app-version v2
    python -m tenderscreen.evaluation.trulens_eval --app-version v1 --prompt-version v1
    python -m tenderscreen.evaluation.trulens_eval --app-version v2_k2 --set RETRIEVAL_K=2
    python -m tenderscreen.evaluation.trulens_eval --leaderboard     # compare every recorded version

Results: TruLens database results/trulens.sqlite (open it with the TruLens
dashboard if installed), plus results/trulens_leaderboard.json.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import warnings

warnings.filterwarnings("ignore")


def _feedbacks():
    from trulens.core import Feedback

    from . import metrics

    def _handoff(output: str) -> dict:
        return json.loads(output)["first_handoff"]

    def groundedness(output: str) -> float:
        return float(metrics.groundedness(_handoff(output)) or 0.0)

    def context_relevance(output: str) -> float:
        d = json.loads(output)
        return float(metrics.context_relevance(d["first_handoff"], set(d["supporting_passages"])) or 0.0)

    def faithfulness(output: str) -> float:
        return float(metrics.faithfulness(_handoff(output)) or 0.0)

    def hallucination_rate(output: str) -> float:
        return float(metrics.hallucination_rate(_handoff(output)) or 0.0)

    def route_correct(output: str) -> float:
        d = json.loads(output)
        return 1.0 if d["route"] == d["expected_route"] else 0.0

    return [
        Feedback(groundedness, name="groundedness").on_output(),
        Feedback(context_relevance, name="context_relevance").on_output(),
        Feedback(faithfulness, name="faithfulness").on_output(),
        Feedback(hallucination_rate, name="hallucination_rate", higher_is_better=False).on_output(),
        Feedback(route_correct, name="route_accuracy").on_output(),
    ]


def run(app_version: str, prompt_version: str, db_path: str, llm_url: str | None = None,
        demo_fault: str | None = None, scenario_file: str = "eval/scenarios.json") -> None:
    from trulens.apps.basic import TruBasicApp
    from trulens.core import TruSession

    from ..agents.pipeline import Pipeline
    from ..api.contracts import validate_bid
    from ..common.config import get_settings
    from ..common.runstore import MemoryTrace
    from ..llm.client import GatewayClient, InProcessStubClient
    from ..retrieval.store import CorpusIndex
    from .run import build_bid

    s = get_settings()
    if not os.path.exists(os.path.join(s.index_dir, "meta.json")):
        from ..retrieval.ingest import run as ingest
        ingest()
    scenarios = {x["id"]: x for x in json.load(open(scenario_file, encoding="utf-8"))["scenarios"]}
    llm = GatewayClient(llm_url, s.llm_call_timeout_s) if llm_url else InProcessStubClient()
    pipe = Pipeline(CorpusIndex(), llm, MemoryTrace())

    def screen(scenario_id: str) -> str:
        scn = scenarios[scenario_id]
        bid, errors = validate_bid(build_bid(scn, s.examples_dir))
        if bid is None:
            raise ValueError(errors)
        opts = {"prompt_version": prompt_version, **({"demo_fault": demo_fault} if demo_fault else {})}
        result, handoffs = pipe.run(bid.model_dump(mode="json"), opts, run_id=f"tl-{app_version}-{scenario_id}")
        return json.dumps({"scenario": scenario_id, "route": result["route"], "expected_route": scn["expected_route"],
                           "verified": result.get("verified"), "attempts": len(handoffs),
                           "supporting_passages": scn["supporting_passages"], "first_handoff": handoffs[0]},
                          default=str)

    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    session = TruSession(database_url=f"sqlite:///{db_path}")
    app = TruBasicApp(screen, app_name="TenderScreen", app_version=app_version, feedbacks=_feedbacks())
    import contextlib
    import io
    for sid in scenarios:
        with contextlib.redirect_stdout(io.StringIO()):  # TruLens prints every call; keep the batch output readable
            with app as recording:  # noqa: F841 - the context manager records the call
                app.app(sid)
    print(f"  recorded {len(scenarios)} scenarios as app_version={app_version}", flush=True)
    app.wait_for_feedback_results()
    # feedback is evaluated asynchronously: wait until every record of this version has all five scores
    import time
    deadline = time.time() + 300
    while time.time() < deadline:
        df, cols = session.get_records_and_feedback()
        mine = df[df["app_version"] == app_version]
        if len(mine) >= len(scenarios) and not mine[cols].isna().any().any():
            break
        time.sleep(2)


def leaderboard(db_path: str, out_dir: str) -> dict:
    from trulens.core import TruSession

    session = TruSession(database_url=f"sqlite:///{db_path}")
    lb = session.get_leaderboard()
    table = json.loads(lb.reset_index().to_json(orient="records"))
    with open(os.path.join(out_dir, "trulens_leaderboard.json"), "w", encoding="utf-8") as fh:
        json.dump(table, fh, indent=1)
    return {"leaderboard": table}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Evaluation set recorded and scored with TruLens")
    ap.add_argument("--app-version", default="v2")
    ap.add_argument("--prompt-version", default=None)
    ap.add_argument("--set", action="append", default=[], help="override a setting, e.g. RETRIEVAL_K=2")
    ap.add_argument("--llm-url", default=None)
    ap.add_argument("--demo-fault", choices=["unsupported_citation"])
    ap.add_argument("--db", default="results/trulens.sqlite")
    ap.add_argument("--out", default="results")
    ap.add_argument("--leaderboard", action="store_true", help="only print the leaderboard of recorded versions")
    args = ap.parse_args(argv)
    for kv in args.set:
        k, v = kv.split("=", 1)
        os.environ[k] = v
    logging.disable(logging.WARNING)
    os.makedirs(args.out, exist_ok=True)
    if not args.leaderboard:
        from ..common.config import get_settings
        pv = args.prompt_version or get_settings().prompt_version
        run(args.app_version, pv, args.db, args.llm_url, args.demo_fault)
    res = leaderboard(args.db, args.out)
    cols = ["app_version", "groundedness", "context_relevance", "faithfulness", "hallucination_rate",
            "route_accuracy", "latency"]
    print("| " + " | ".join(cols) + " |")
    print("|" + "---|" * len(cols))
    for row in res["leaderboard"]:
        print("| " + " | ".join(str(round(row[c], 4)) if isinstance(row.get(c), float) else str(row.get(c))
                                for c in cols) + " |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
