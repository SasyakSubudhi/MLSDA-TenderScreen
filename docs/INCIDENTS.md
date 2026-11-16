# Incident log

Real failures met while building and testing the system, newest first.

---

### INC-10 - 2026-11-12 - Live model run: truncated prompts and drafting timeouts

* **What happened:** the first evaluation run against a real local model
  (Ollama, CPU only) produced a gateway `500` after 3 minutes on the first
  drafting call and Ollama logged `truncating input prompt limit=2048
  prompt=2327`.
* **Cause:** two things. Ollama's default context window is 2,048 tokens, but
  the drafting prompt (all findings with their quoted evidence) is ~2,300-2,550
  tokens, so its beginning - the instructions - was being cut off. And with no
  output cap, a small model kept generating towards the 800-token limit, which
  on 2 vCPUs exceeded the provider timeout.
* **Fix:** the local model is served with a 4,096-token context (Ollama
  Modelfile `PARAMETER num_ctx 4096`); every model call now has a per-task
  output cap (`plan` 200, `derive_queries` 300, `draft` 900 tokens) - a
  latency and cost control in its own right; live-model timeouts are set as
  non-secret configuration (shell or `.env`, documented in README).
* **Lesson:** the stub never exercises context limits or generation length;
  a live-model rehearsal is needed before trusting the timeouts.

### INC-09 - 2026-11-12 - TruLens leaderboard read before feedback finished

* **What happened:** the first TruLens batch for prompt v1 reported route
  accuracy 0.7778 while the same runs scored 21/28 = 0.75 in our own runner.
* **Cause:** TruLens evaluates feedback functions asynchronously; the
  leaderboard was read while the last record's feedback was still pending, so
  the mean was over 27 records.
* **Fix:** after recording, the runner waits until every record of the version
  has all five feedback scores before reading the leaderboard. Both runners
  now agree exactly.

### INC-08 - 2026-11-12 - GFR Rule 170 quoted without the words that matter

* **What happened:** the MSE EMD-exemption conflict cited GFR Rule 170 with the
  quote "...Bid Security (also known as Earnest Money) is to be obtained from
  the" - cut just before "bidders except Micro and Small Enterprises".
* **Cause:** Rule 170(i) is one 800-character sentence; long sentences were
  trimmed to a window centred on the *topic* words (bid security), not on the
  words that make it a relaxation.
* **Fix:** long policy sentences are now centred on the relaxing words
  ("except", "relaxed", "exempt"). A unit test asserts the quote contains
  "except Micro and Small Enterprises".

### INC-07 - 2026-11-12 - EMD amount lost from the Annexure-VIII table

* **What happened:** after switching to the CPPP-published tender, every run
  returned C3 EMD = EVIDENCE_MISSING and was escalated, although both the EMD
  clause and Annexure-VIII had been retrieved.
* **Cause:** Annexure-VIII is a one-row table, not prose. The sentence splitter
  cut it at "11 nos. 8.8", so the quote handed to the parser ended before the
  amount "17600".
* **Fix:** the table is quoted whole (from the heading to the EMD figure); the
  amount is parsed from that quote. All seven example bids then routed
  correctly; a unit test now checks that 17,600 is read from the tender text.
* **Lesson:** "quote a sentence" is the wrong unit for tables.

### INC-06 - 2026-11-12 - Load-test results could not be written (Locust container)

* **What happened:** the first `docker compose --profile loadtest run --rm
  locust ... --csv /mnt/locust/results/smoke` crashed at start-up with
  `PermissionError: [Errno 13] Permission denied: '/mnt/locust/results/smoke_stats.csv'`.
* **Cause:** the official Locust image runs as an unprivileged user (uid
  1000); the bind-mounted `loadtest/results` folder on the Linux test host was
  owned by root.
* **Fix:** the `locust` service runs as `user: "0"` (it is a test tool on the
  local network only). Docker Desktop on Windows/macOS was not affected.
* **Lesson:** bind-mount permissions differ between Docker Desktop and Linux
  hosts; test the load profile on Linux too.

### INC-05 - 2026-11-12 - Provider outage invisible during the breaker test

* **What happened:** with the stub switched to `down`, three consecutive
  screenings of the same example bid still returned *pass to technical
  evaluation* in ~0.04 s and the breaker stayed closed.
* **Detection:** reliability demo rehearsal; `/v1/system` showed
  `consecutive_failures: 0`.
* **Cause:** working as designed but surprising - every model call of those
  runs was answered by the gateway's response cache (identical prompts at
  temperature 0), so no call reached the provider.
* **Fix / procedure change:** the demo and runbook use a bid with new figures
  (uncached prompts); load tests run with `LLM_CACHE_ENABLED=false`; the
  dashboard shows the cache hit ratio next to the breaker state. With an
  uncached bid the breaker opened on the second run and subsequent runs failed
  fast (0.05 s) into the degraded escalation path.
* **Lesson:** a cache can hide an outage; health must be judged on provider
  calls, not on answers.

### INC-04 - 2026-11-12 - Letter-spaced GFR headings merged into one word

* **What happened:** unit test `test_normalisation_joins_letter_spaced_headings`
  failed: "R e s e r v e d  I t e m s" was normalised to `ReservedItems`.
* **Cause:** `normalize_text` collapsed whitespace *before* joining
  letter-spaced words, destroying the double space that separates words in
  the GFR PDF's spaced headings.
* **Fix:** join first, collapse second; clause registry rebuilt. Running the
  indexer on the existing index re-embedded only the 21 of 165 chunks whose
  text changed and patched them into FAISS in place (mode `incremental`,
  21 deleted + 21 added, 7.4 s) - no full rebuild.
* **Lesson:** the "quote must be verbatim" rule makes normalisation part of
  the contract between ingestion and the verifier; it needs its own tests.

### INC-03 - 2026-11-12 - Garbled citation from the scanned 25-07-2016 OM

* **What happened:** an MSE bidder's turnover relaxation was cited with the
  quote "...may relax condition of prior turnove-r--ancl-Ki-orexperience...",
  and for the turnover condition the MSE relaxation was not found at all.
* **Cause:** OCR noise in the Ministry of Coal scan; the word "turnover"
  was broken, so the topic pattern missed the sentence.
* **Fix:** registered the clean reprint of the same paragraph from the DPE
  compendium (`DOE-2016-07-REPRINT`) as a clause unit; quotes are ranked by an
  OCR-noise score and near-duplicate copies are de-duplicated, so the clean
  copy is cited. Scenarios S13 and S26 cover MSE relaxation.
* **Lesson:** "prefer documents with a text layer" is not enough - a text
  layer can itself be bad OCR.

### INC-02 - 2026-11-12 - Metrics would split across API processes

* **What happened:** during review of the first Compose run, the API was
  started with `uvicorn --workers 2`. `prometheus_client` keeps counters per
  process, so each scrape would see one process's counters at random, making
  request and quarantine counts jump.
* **Fix:** the API runs one uvicorn process (async; it only validates and
  enqueues). Scaling the API horizontally means more containers, each scraped
  separately.

### INC-01 - 2026-11-11 - Embedding library would not install on the dev machine

* **What happened:** `pip install fastembed==0.4.2` failed on the developer
  machine: no distribution for Python 3.13.
* **Cause:** the dev machine had Python 3.13; the ONNX runtime wheels used by
  fastembed lagged behind.
* **Fix:** development pinned to Python 3.12 - the same version as the
  container base image (`python:3.12-slim`) - and fastembed to 0.5.1.
* **Lesson:** develop on the container's Python version.
