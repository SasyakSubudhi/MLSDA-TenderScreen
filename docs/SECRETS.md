# Secrets, ports and shared deployments

TenderScreen needs **no credential** for its default configuration (built-in stub model)
or for the optional local Ollama model. A secret only exists if you choose a hosted
model provider (LLM API key) or run a shared deployment (Grafana admin password).

## Rules

* `.env.example` is tracked and holds non-secret defaults only; it is the only env file
  injected into the application containers.
* `.env` (git-ignored) is read by Compose **only** to fill `${VAR}` references in
  `docker-compose.yml` (the `x-tunables` block, `API_PORT`, `AGENT_WORKERS`,
  `OLLAMA_MODEL`). It is never injected into a container, so a key placed there would
  not reach any service and must not be used.
* Secrets are files under `secrets/` (git-ignored and docker-ignored), mounted by
  Compose at `/run/secrets/<name>` into **one** service each.

## Hosted model provider

```
mkdir secrets
printf '%s' 'PLACEHOLDER-KEY' > secrets/llm_api_key      # use your real key here, never in git
LLM_PROVIDER_NAME=<provider> LLM_BASE_URL=<https://.../v1> LLM_MODEL=<model id> \
  docker compose -f docker-compose.yml -f docker-compose.hosted.yml up -d
```

`docker-compose.hosted.yml` mounts `llm_api_key` in `llm-gateway` only and sets
`LLM_API_KEY_FILE=/run/secrets/llm_api_key`.

Key resolution in `tenderscreen/llm/gateway.py` (`resolve_api_key`):

1. `LLM_API_KEY_FILE` set -> the file's trimmed contents are used and `LLM_API_KEY` is
   ignored. If the file cannot be read or is empty the gateway refuses to start with
   `LLM_API_KEY_FILE is set but the file cannot be read (<ErrorType>)` or
   `... the file is empty`. The path's contents never appear in the message.
2. otherwise `LLM_API_KEY` (a direct environment variable, for runs outside Compose).
3. otherwise no key: the stub, or a keyless endpoint such as Ollama.

Provider failures reach callers, logs, traces and metrics as a stable code plus the
upstream HTTP status (`provider_rejected_request:401`); the upstream response body is
dropped. Retries, the circuit breaker and degraded routing are unchanged.

## Ports and the Docker socket

`api` (8000), `prometheus` (9090), `grafana` (3000) and `jaeger` (16686) are published
on `127.0.0.1` only. Use an SSH tunnel (`ssh -L 8000:127.0.0.1:8000 host`) to reach a
remote machine.

The raw Docker socket is mounted into one container only, `docker-proxy`
(`tecnativa/docker-socket-proxy`), read-only and limited to `GET /containers/*`
(POST, exec, images, volumes, networks and the rest are off). It is not published and
nothing else is on its network path except `resource-exporter`, which reads container
CPU / memory / uptime / restarts through `DOCKER_API_URL=http://docker-proxy:2375` and
runs as a normal user. The proxy is still a trusted component: do not expose its port
or attach other services to it. If you do not want Docker access at all, run
`docker compose stop docker-proxy resource-exporter` (the container panels in Grafana
then show no data; everything else works).

## Shared deployments

`docker-compose.shared.yml` turns off the demo mutation routes
(`POST /v1/admin/provider-mode`, `POST /v1/admin/breaker-reset` return 403) and Grafana
anonymous access, and reads the Grafana admin password from `secrets/grafana_admin_password`
through `GF_SECURITY_ADMIN_PASSWORD__FILE` (Grafana only).

```
mkdir -p secrets && printf '%s' 'PLACEHOLDER-PASSWORD' > secrets/grafana_admin_password
docker compose -f docker-compose.yml -f docker-compose.shared.yml up -d
```

The API has no built-in authentication. For access by other people put an authenticating
reverse proxy (TLS plus login or SSO) in front of the loopback ports, or use per-user SSH
tunnels. Never publish these ports on `0.0.0.0`. Combine overrides when needed:
`-f docker-compose.yml -f docker-compose.hosted.yml -f docker-compose.shared.yml`.

## Windows load tests

`scripts\windows\3_RUN_LOAD_TESTS.cmd` does not touch `.env`. It writes a temporary
non-secret env file in `%TEMP%` (stub selection, worker count, cache flags, rate limit),
passes it with `--env-file`, clears any provider variables in its own shell, exits non-zero
on any failed step, and removes the temporary file and restores the default stack on both
the success and failure paths.

## Hygiene checks

* Tests use synthetic credentials only (`sk-synthetic-canary-0000`).
* Before sharing a repo: `git ls-files | grep -E '(^|/)(\.env$|secrets/)'` must print nothing,
  and `git grep -nE '(sk|gsk|AIza)[-_A-Za-z0-9]{20,}' $(git rev-list --all)` should find no real key.
