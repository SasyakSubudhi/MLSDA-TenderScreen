# Evaluation (L2, L3, L5, L6)

## The evaluation set

[eval/scenarios.json](../eval/scenarios.json) - **28 scenarios** for tender
IITPKD/AR/RS/053/2018-19, each with the expected route, the expected status
of the conditions it is about, and the supporting passages (clause ids in
`corpus/clauses.json`). Each scenario is one of the example bids plus field
overrides. Coverage:

| Group | Scenarios |
|---|---|
| ordinary pass, boundaries | S01, S23 (exactly Rs.5,00,000), S24 (Rs.1 below), S27 (OEM needs no OEM authorisation) |
| **section 4.1 reference case** and startup variants | **S02**, S03, S04, S05, S14, S20, S22, S25 |
| rejections with a cited condition | S06 (turnover), S07 (no CFTI supply), S08/S09 (EMD), S16 (outside the five states) |
| MSE: EMD paid, **EMD exemption vs GFR 170 conflict**, missing certificate, relaxation conflicts | S10, **S11**, S12, S13, S28 |
| curable gaps -> clarification | S12, S14, S15, S26 |
| case the corpus cannot answer, unclear category | S17 (consortium), S18 (foreign), S19, S21 |

Run as a batch from the command line - with our runner, or recorded and
scored with **TruLens**:

```bash
docker compose exec --index 1 agent-worker python -m tenderscreen.evaluation.run --compare v1 v2
docker compose exec --index 1 agent-worker python -m tenderscreen.evaluation.trulens_eval --app-version v2
docker compose exec --index 1 agent-worker python -m tenderscreen.evaluation.trulens_eval --app-version v1 --prompt-version v1
docker compose exec --index 1 agent-worker python -m tenderscreen.evaluation.trulens_eval --leaderboard
```

Results in this repository: `eval/results/eval_<label>.json` (per scenario
and aggregate), `eval/results/trulens.sqlite` (the TruLens database: every
record and feedback result) and `eval/results/trulens_leaderboard.json`.
Unless stated otherwise the numbers below use the stub model and the default
local embeddings.

## How each metric is computed

The metrics are TruLens `Feedback` functions
(`tenderscreen/evaluation/trulens_eval.py`) whose implementations are
deterministic (`tenderscreen/evaluation/metrics.py`). They are scored on the
research agent's **draft** - before verification - so the effect of the
verifier is visible; the runner also scores the final answer.

| Metric | Definition |
|---|---|
| **groundedness** | share of citations whose chunk was retrieved in this run **and** whose quoted text occurs verbatim in that chunk |
| **context relevance** | share of retrieved passages that carry the clause tag of something the run depends on (the governing clauses of the five conditions, plus the scenario's labelled supporting passages) |
| **faithfulness** | share of findings whose status the rule engine reproduces when given **only the passages the findings cite** - the claim follows from its citations |
| **hallucination rate** (lower is better) | share of findings with any unsupported element: a citation that does not resolve, an evidence id in the text that is not the finding's own, or a number in the text found neither in the bid nor in the cited quotes |

Also reported: route accuracy, condition-status accuracy, expected-passage
recall (share of the scenario's supporting clauses retrieved), verified rate.

Why deterministic feedback rather than an LLM judge: the judge would be the
same kind of model being judged, its score varies run to run, and on a
free tier it doubles the calls. These definitions are exact, repeatable and
match what the verifier enforces.

## TruLens leaderboard (all recorded versions, 28 records each)

| app version | groundedness | context relevance | faithfulness | hallucination rate | route accuracy |
|---|---|---|---|---|---|
| v1 (baseline prompt, safeguard off) | 1.000 | 0.720 | 1.000 | 0.000 | 0.750 |
| **v2** (default) | 1.000 | 0.679 | 1.000 | 0.000 | **1.000** |
| v2, top-k 2 | 1.000 | 0.954 | 1.000 | 0.000 | 1.000 |
| v2, faulty model | 0.937 | 0.679 | 1.000 | 0.167 | 1.000 |

## Prompt variants compared (L2)

Prompts are files in `prompts/v1` and `prompts/v2`; the version recorded with
every model call and result is `<version>@<sha256 of the templates>`.

* **v1 (baseline, single-pass style)** - query derivation restates the
  condition and searches the tender only; loose drafting rules.
* **v2** - query derivation is told that central rules (GFR, DoE OMs) can
  relax or exempt a tender condition for startups/MSEs and must search the
  policy family in the policy documents' vocabulary; strict citation and
  number rules for drafting.

The comparison below isolates the prompt: it is run with the policy safeguard
off (`AGENT_POLICY_SAFEGUARD=false`, see *Policy safeguard* further down), so
the only thing that can bring a central rule into the evidence is the
prompt's own query derivation.

| metric | v1 | v2 |
|---|---|---|
| **route accuracy** | 0.750 (21/28) | **1.000 (28/28)** |
| condition-status accuracy | 0.804 | **1.000** |
| expected-passage recall | 0.857 | **1.000** |
| verified rate | 1.0 | 1.0 |
| groundedness / faithfulness (draft) | 1.0 / 1.0 | 1.0 / 1.0 |
| context relevance (draft) | 0.720 | 0.679 |
| hallucination rate (draft) | 0.0 | 0.0 |
| mean tool calls / LLM calls per run | 8.5 / 3.5 | 9.3 / 3.7 |
| mean prompt tokens per run | 3,094 | 3,531 |
| cost per 100 runs (list price) | USD 0.049 | USD 0.055 |
| **section 4.1 case (S02)** | **reject** (wrong) | **escalate** (correct) |

**v2 is better, on route accuracy by 25 percentage points (0.750 -> 1.000).**
v1 got all seven cases that turn on a central rule wrong (S02, S03, S04, S11,
S13, S25, S28): it never searched the policy documents, so the rule engine
saw only the tender and recommended rejection - grounded, faithful,
well-cited, and wrong. That is exactly the failure section 3 of the brief
predicts. v2 pays ~14 % more tokens and 4 points of context relevance for it.
Groundedness, faithfulness and hallucination do not separate the two: both
drafts cite only what was retrieved; the difference is *what* was retrieved.

## Deliberate change: top-k 4 -> 2 (L3)

One change, everything else fixed (prompt v2): `RETRIEVAL_K` from 4 to 2.

| metric | before (k=4) | after (k=2) |
|---|---|---|
| groundedness (draft) | 1.000 | 1.000 |
| **context relevance (draft)** | **0.679** | **0.954** |
| faithfulness (draft) | 1.000 | 1.000 |
| hallucination rate (draft) | 0.000 | 0.000 |
| route accuracy | 1.000 | 1.000 |
| expected-passage recall | 1.000 | 1.000 |
| mean tool calls per run | 9.3 | 12.3 |
| mean LLM calls per run | 3.7 | 4.9 |
| cost per 100 runs | USD 0.055 | USD 0.063 |

Context relevance rose by 27.6 points with no loss of recall or accuracy, but
the agent needed three more searches per run to find every governing clause
(the second search is the keyword retry), which cost one more planning call
and 15 % more. We keep **k=4** as the default: the precision gain does not
change any answer, and fewer, larger searches are cheaper and leave more
recall margin for a live model's less predictable queries.

## Seeing the verifier work: a faulty model

The stub follows instructions perfectly, so its drafts score 1.0 / 0.0. To
show that the metrics - and the verifier - catch a model that does not, the
batch was rerun with `--demo-fault unsupported_citation`: every first draft
cites a passage that was never retrieved.

| metric | draft (before verification) | final (after verification + retry) |
|---|---|---|
| groundedness | 0.937 | 1.000 |
| hallucination rate | 0.167 (one finding in six) | 0.000 |
| faithfulness | 1.000 | 1.000 |
| route accuracy | - | 1.000 |
| mean tool calls per run | 9.3 (clean run) | 23.3 (incl. retry) |

All 28 bad drafts were caught by check V2 and retried with the keyword /
clause-lookup strategy; no unverified answer was returned. The retry more
than doubles tool and model calls (cost per 100 runs USD 0.055 -> 0.114).

## Live model run

All 28 scenarios were run through the real LLM gateway on 12 Nov 2026 with a
small open-weight instruct model (about 1.5 B parameters, 4-bit) served by
Ollama **on the team's laptop CPU** (`RUN_LIVE_MODEL_EVALUATION.cmd`, which
lives outside the repository so that the repository names no model). Same
prompts (v2), same retrieval, same verifier; response cache off; cost 0
(local model). Raw output: `eval/results/eval_v2_live_local_model.json`.

| metric | stub (v2) | live local model (v2) |
|---|---|---|
| route accuracy | 1.000 | **0.964** (27/28) |
| status accuracy | 1.000 | 0.964 |
| expected-passage recall | 1.000 | 0.982 |
| verified rate | 1.000 | 1.000 |
| groundedness (final) | 1.000 | 1.000 |
| context relevance (final) | 0.679 | 0.661 |
| faithfulness (final) | 1.000 | 1.000 |
| hallucination rate (final) | 0.000 | 0.000 |
| mean model calls per run | 3.7 | 4.1 |
| mean prompt tokens per run | 3,531 | 4,374 |
| median run time | 0.3 s | 113 s (CPU) |

The section 4.1 reference case (S02) was escalated correctly, with the DoE
OM 2016 relaxation cited against the tender's turnover clause.

**The one miss - S11 (MSE claiming the EMD exemption).** The model's derived
queries for the EMD condition did not surface GFR Rule 170(i), so the rule
engine saw only the tender's "bids without EMD will be rejected" clause and
returned UNMET -> *reject with cited condition* instead of CONFLICT ->
*escalate*. The verifier passed it, correctly by its own definition: every
citation was real and the status followed from the cited passages. What it
cannot detect is evidence that was never retrieved. This is exactly the
failure the brief warns about - a relaxation that the request text does not
lead to - and it shows that the stub's 1.0 depends on good query derivation.
**Fix (implemented): the policy safeguard.** When a bidder claims a category
(startup or MSE), the research agent now always adds one fixed policy-family
search for that category and condition, next to whatever queries the model
derived (`reflect()` in `tenderscreen/agents/research.py`; setting
`AGENT_POLICY_SAFEGUARD`, on by default). The relaxation therefore reaches
the rule engine even when the model's own queries miss it. It costs about
0.3 extra retrieval calls per run and no extra model call.

One run (S24) took 75 minutes of wall-clock time because the laptop slept
during it; the run itself was correct and is excluded from the median.

## Policy safeguard: with and without

The S11 miss is reproduced deterministically by the baseline prompt v1, whose
query derivation never searches the policy documents. Same 28 scenarios:

| | safeguard off | safeguard on (default) |
|---|---|---|
| prompt v1 route accuracy | 0.750 (21/28) | **1.000 (28/28)** |
| prompt v2 route accuracy | 1.000 (28/28) | 1.000 (28/28) |

With the safeguard on, all seven scenarios that v1 got wrong (S02, S03, S04,
S11, S13, S25, S28) are routed correctly, each citing the central rule. The
unit test `test_policy_safeguard_catches_a_relaxation_the_model_queries_miss`
pins this for S11: safeguard off -> *reject with cited condition*; on ->
*escalate to human* with GFR Rule 170 cited. `2_RUN_EVALUATION.cmd` runs both
ablations (`v1_no_safeguard`, `v2_no_safeguard`) and records them in TruLens.

The live-model figures above (27/28) were measured on 12 Nov, before the
safeguard existed; `RUN_LIVE_MODEL_EVALUATION.cmd` re-measures them.

## Cost (L5)

Tokens and cost are recorded at call time by the gateway, labelled by agent,
task (step) and prompt version, and per run in the trace.

* **Cost per 100 runs: USD 0.055** (prompt v2, list price USD 0.10 / 0.40 per
  1M input / output tokens, ~3,530 prompt tokens and ~3.7 model calls per run).
  On a free tier or a local model the cash cost is 0; this is the list-price
  equivalent.
* **Largest single cost driver: the drafting call (`draft`) - 66 % of cost**,
  then planning 29 %, query derivation 5 %. Drafting carries every finding
  with its quoted evidence; planning is several small calls.
* The LLM response cache cuts cost per run under load (see LOADTEST.md).

## Live quality over time (L6)

The verifier scores a sample of live runs (`LIVE_EVAL_SAMPLE_RATE`, default
25 %) with the same metric functions (no labels needed: context relevance
uses the clauses the findings depend on). Scores go to the
`live_quality_score` / `live_quality` Prometheus metrics (Grafana panel
*Live quality score*, quality over time) and to `GET /v1/quality`. User
feedback (`POST /v1/screenings/{id}/feedback`) is stored, counted by route,
and summarised at `GET /v1/feedback/summary`.
