# Service level objective (declared before any load test)

**Declared:** 2026-11-12, before the first Locust run. Results are in
[LOADTEST.md](LOADTEST.md); this file was not edited after seeing them.

## Objective

> **p95 of the complete-run latency is at most 30 seconds** while 10 clients
> concurrently submit bids and wait for their results, with the model provider
> replaced by the stub (mean 0.8 s, sd 0.2 s per call).

*Complete-run latency* is measured from the moment the client sends
`POST /v1/screenings` to the moment `GET /v1/screenings/{id}` first returns
the final result - queue wait, research agent, hand-off, verifier and any
verification retry included. The fast 202 response of the submission call
alone is **not** the measured quantity.

A run that finishes but is escalated because it hit its deadline counts as a
**failure** for this objective (it did not produce an answer in time).

## Why 30 seconds (justified against section 1 of the brief)

* Screening is a back-office step before technical evaluation. Nobody is
  waiting at a checkout; a purchase officer is screening a stack of bids,
  usually just after the closing time, and reviews each finding on screen.
* An officer can stay on one bid for half a minute while the finding is
  produced without losing context. Beyond that, people switch tasks and the
  tool stops feeling interactive. The brief itself expects runs to take "tens
  of seconds".
* Bids arrive in a burst at the deadline: a tender like IITPKD/AR/RS/053/2018-19
  draws tens of bids, so 10 concurrent screenings is a realistic peak for one
  procurement cell; it is not a public-facing load.
* Correctness is worth more than speed here: a rejection must survive
  challenge, so the target leaves room for the policy search, the
  cross-reference and a verification retry, not just one LLM call.
* The hard per-run deadline (`RUN_TIMEOUT_S` = 90 s) is a separate safety
  limit; the SLO is deliberately tighter than the deadline.

## How it is measured

`loadtest/locustfile.py` (Locust, in the `locust` container) submits a bid
with randomised figures (so whole-run and LLM caches cannot hide the work),
polls every second until the final result arrives, and reports the whole run
as a custom `E2E complete run` request. `loadtest/run_matrix.py` repeats it for
1, 5, 10, 20 and 40 users with 1 and 3 research workers
(`docker compose up -d --scale agent-worker=N`).
