# Windows helper scripts

Double-click in this order (Docker Desktop must be running):

| Script | What it does | Time |
|---|---|---|
| `1_START_SYSTEM.cmd` | `docker compose up --build`, waits until the agents are ready, opens the web client and Grafana | 3-6 min first time, <1 min after |
| `2_RUN_EVALUATION.cmd` | 28-scenario evaluation: prompt v1 vs v2, top-k change, faulty-model check, all recorded in TruLens; results copied to `eval\results` | ~3 min |
| `3_RUN_LOAD_TESTS.cmd` | Locust matrix (1 and 3 research workers x 1-40 users) and the LLM-cache experiment; results in `loadtest\results` | ~35 min |
| `4_STOP_SYSTEM.cmd` | stops everything (keeps data) | seconds |

All of it runs inside Docker on this computer.
