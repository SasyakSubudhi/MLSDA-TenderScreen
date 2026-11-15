# Load test results (D6, A7, D7)

Target, declared beforehand in [SLO.md](SLO.md): **complete-run p95 <= 30 s
at 10 concurrent users.**

Measured on 12 Nov 2026 on the team's Windows 11 laptop (Docker Desktop,
WSL 2 backend), all services under Docker Compose, started with
`scripts\windows\3_RUN_LOAD_TESTS.cmd`. The model is replaced by `llm-stub`
(OpenAI-compatible, 0.8 s mean, 0.2 s sd per call) so that the test measures
our system rather than a provider's rate limit; `RATE_LIMIT_PER_MIN=0` and
`LLM_CACHE_ENABLED=false` so neither the limiter nor the cache distorts the
measurement. Locust runs in its own container on the compose network; each
user submits a bid with randomised turnover figures, polls every second until
the final result (custom `E2E complete run` event), waits 1-3 s and repeats;
90 s per cell, in-flight runs allowed to finish. Per-container CPU comes from
our `resource-exporter` (Docker API -> Prometheus), peak over each cell.
Raw output: `loadtest/results/` (`matrix.json` with per-container peak CPU,
`matrix.md`, `cache_experiment.json`, Locust CSVs).

## Results - complete runs, submission to final result

| research workers | users | completed runs | failures | runs/s | p50 (s) | p95 (s) | SLO p95 <= 30 s |
|---|---|---|---|---|---|---|---|
| 1 | 1 | 17 | 0 | 0.19 | 3.0 | 5.1 | met |
| 1 | 5 | 32 | 0 | 0.32 | 13.0 | 16.0 | met |
| 1 | **10** | 38 | 0 | 0.34 | 26.0 | **32.0** | **NOT met** |
| 1 | 20 | 48 | 0 | 0.34 | 56.0 | 58.0 | NOT met |
| 1 | 40 | 67 | 13 | 0.39 | 86.0 | 90.0 | NOT met - 13 runs hit the 90 s deadline |
| 3 | 1 | 16 | 0 | 0.18 | 3.0 | 5.2 | met |
| 3 | 5 | 77 | 0 | 0.83 | 4.0 | 5.1 | met |
| 3 | **10** | 98 | 0 | 1.02 | 7.1 | **9.2** | **met** |
| 3 | 20 | 107 | 0 | 1.02 | 17.0 | 19.0 | met |
| 3 | 40 | 125 | 0 | 1.00 | 36.0 | 40.0 | NOT met |

**Verdict:** with the default deployment - **three research workers**
(`deploy.replicas` in `docker-compose.yml`, `AGENT_WORKERS=3`) - the SLO is
**met with a wide margin**: p95 9.2 s at 10 users, and still met at 20 users
(19 s). With a single research worker it is **missed** at the declared load
(p95 32 s at 10 users, 2 s over); one worker is enough only up to about 5-8
users. That measurement is why three workers are the default.

**Where it fails:** 1 worker between 5 and 10 users; 3 workers between 20 and
40 users. At 40 users with 1 worker, 13 runs exceeded the 90 s run deadline;
they were returned as escalations (never as answers) and counted as failures.

## Effect of the number of research workers (1 vs 3)

| | 1 worker | 3 workers | change |
|---|---|---|---|
| saturated throughput | 0.34 runs/s | 1.02 runs/s | x 3.0 |
| p95 at 10 users | 32.0 s | 9.2 s | -71 % |
| highest tested load meeting the SLO | 5 users | 20 users | x 4 |

Scaling is almost exactly linear (x 3.0 throughput for x 3 workers), which
is itself the evidence for where the bottleneck is.

## The bottleneck

* **Component: the research agent worker.** Each replica processes one run at
  a time; a run is about 8 dependent steps including about 4 model calls of
  ~0.8 s, so one replica completes ~1 run per 3 s. Saturated throughput is
  replicas / ~3 s: 0.34 runs/s with 1 and 1.02 runs/s with 3 - what was
  measured. Above that, runs wait in `stream:research` (Grafana *queue depth*
  rises) and latency is almost all queue wait: p50 26 s at 10 users with 1
  worker, while a run alone takes 3 s.
* **Not the bottleneck: CPU.** Under saturating load (10-40 users) no
  container peaked above 0.3 of a CPU core (api 0.17-0.29, llm-gateway
  0.12-0.17, each agent worker 0.12-0.22, verifier 0.12-0.15, Redis < 0.04).
  The workers are I/O-bound, waiting on model calls. (The ~1.3-core peaks in
  the first cells of each block are the worker loading the embedding model at
  container start, not load.)
* **Not the bottleneck:** the verifier (milliseconds per run) or the gateway
  (async; it relayed ~4 calls/s without queueing).
* **Next step if more capacity were needed:** more replicas, or several runs
  concurrently inside one worker (async model calls), since CPU is mostly
  idle. With a real hosted provider, its rate limit becomes the bottleneck
  long before this - which is why load tests use the stub.

## Caching (D7): measured effect on p95 and cost per run

3 research workers, 10 users, 90 s, randomised bids; only the gateway's LLM
response cache toggled; the cache keys in Redis are deleted before each cell.
Cost is the list-price equivalent recorded at call time (USD 0.10 / 0.40 per
1M input / output tokens) over the runs finalised in the window.

| LLM response cache | completed runs | runs/s | p50 | p95 | cache hit ratio | cost per 100 runs |
|---|---|---|---|---|---|---|
| off | 89 | 0.95 | 8.1 s | 11.0 s | 0 % | USD 0.0526 |
| **on** | 237 | 2.67 | 2.0 s | **3.0 s** | 72 % | **USD 0.0399** |

The cache serves prompts that are identical across bids: planning prompts
and query derivation for the same bidder category and clause. Drafting
prompts contain each bid's figures, so they mostly miss - and drafting is the
largest cost driver, which is why cost falls less than latency. Result:
**p95 -73 %, cost per run -24 %, throughput x 2.8** under the same load.
Caching at temperature 0 is safe: an identical prompt gets the same answer,
so serving it from cache changes cost and latency, not the result. The risk -
a cache hiding a provider outage - is recorded in incident INC-05.

Other caches: retrieval results (keyed by index version, so a re-index
invalidates them) and whole runs (only verified, non-degraded results; an
identical resubmission returns HTTP 200 with the earlier run in a few ms).
