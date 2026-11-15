# Runbook - the two most likely failure modes

## 1. Model provider unavailable or rate-limited

The most likely failure: free tiers rate-limit (HTTP 429), keys expire,
providers have outages.

**Symptom**
* Results come back as **Escalate to Tender Committee** with
  `degraded: true` and the reason "the language model was unavailable;
  findings were produced by the rule-based fallback only".
* Grafana: *Circuit breaker state* panel at 2 (open); *Provider calls by
  outcome* shows `provider_unavailable` / `circuit_open`; *Retry rate* rises.

**Confirm**
```bash
curl -s localhost:8000/v1/system            # llm_gateway.breaker.state == "open"
docker compose logs --tail 50 llm-gateway   # "llm call failed" with outcome / last_error
docker compose logs --tail 20 llm-stub      # (stub profile) mode "down" / not running
```
`last_error` tells you which: `http_429` = rate limit, `http_401` = key,
`timeout` / `transport:*` = provider unreachable.

**Recover**
1. Nothing to do for a short outage: the breaker half-opens after
   `BREAKER_OPEN_SECONDS` (20 s) and closes on the first successful probe.
2. Rate limit: lower load (the API rate limit `RATE_LIMIT_PER_MIN`), keep
   `LLM_CACHE_ENABLED=true`, or wait for the quota window.
3. Key / provider problem: fix `.env` (`LLM_API_KEY`, `LLM_BASE_URL`,
   `LLM_MODEL`), then `docker compose up -d llm-gateway`. To keep working
   during an outage, switch to the stub by removing those lines from `.env`.
4. Stub profile: `docker compose start llm-stub` or
   `curl -X POST localhost:8000/v1/admin/provider-mode -H "Content-Type: application/json" -d "{\"mode\":\"ok\"}"`.
5. Optionally close the breaker at once: `curl -X POST localhost:8000/v1/admin/breaker-reset`.
6. Re-submit the degraded runs - degraded results are never cached, so a
   re-submission is screened afresh.

## 2. Runs stuck in the queue (agent worker down, or index missing)

**Symptom**
* Status stays `queued` or `researching`; after 1.5 x the run deadline the
  status response carries `"overdue": true`.
* Grafana: *Queue depth* rising, *Throughput* at 0, *Availability* of
  `agent-worker` below 1; web client header shows `workers: research 0`.

**Confirm**
```bash
curl -s localhost:8000/v1/system      # workers{} has no "research:*" entry; queues.research > 0
docker compose ps                     # agent-worker exited / unhealthy?
docker compose logs --tail 50 agent-worker   # crash trace, or "waiting for index"
docker compose logs indexer           # did ingestion finish with exit 0?
```

**Recover**
1. Worker crashed: `docker compose up -d agent-worker` (or
   `--scale agent-worker=3`). Messages a dead worker was holding are
   reclaimed automatically after `QUEUE_LEASE_MS` (60 s) - no manual replay.
2. Worker logs say "waiting for index": run `docker compose run --rm indexer`
   and check it exits 0; workers pick the index up without a restart. If the
   model download failed (no internet), the indexer falls back to hash
   embeddings when `EMBEDDING_FALLBACK=hash`; check
   `python -m tenderscreen.retrieval.ingest --check` for `embedding_backend`.
3. Verifier down (runs stuck in `verifying`): `docker compose up -d verifier-worker`.
4. Poison message: after 3 deliveries a message is moved to
   `stream:dead-letter` and its run is returned as *escalated - failed*.
   Inspect with `docker compose exec redis redis-cli XRANGE stream:dead-letter - +`.

## Useful commands

```bash
docker compose ps
docker compose logs -f api agent-worker verifier-worker llm-gateway
curl -s localhost:8000/v1/screenings/<run_id>/trace     # reconstruct one run
curl -s localhost:8000/v1/quarantine                    # rejected requests
```
