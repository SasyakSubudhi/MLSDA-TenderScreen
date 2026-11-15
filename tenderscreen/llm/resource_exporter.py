"""resource-exporter - CPU, memory, uptime and restarts for EVERY container of
this Compose project (requirements D5 and A6), including redis, prometheus,
grafana and jaeger, which do not expose process metrics themselves.

It reads the Docker Engine API through the read-only mounted socket
(/var/run/docker.sock) every SCRAPE_EVERY_S seconds and exports:

    container_cpu_cores{service, container}        CPU cores in use (1.0 = one full core)
    container_memory_bytes{service, container}     memory in use (page cache excluded)
    container_memory_limit_bytes{...}              memory limit visible to the container
    container_uptime_seconds{service, container}   seconds since the container started
    container_restarts{service, container}         restart count
    container_up{service, container}               1 if running

Port: METRICS_PORT (default 9103).
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from datetime import datetime, timezone

import httpx
from prometheus_client import Gauge, start_http_server

SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
EVERY = float(os.environ.get("SCRAPE_EVERY_S", "5"))
LABELS = ["service", "container"]
CPU = Gauge("container_cpu_cores", "CPU cores used by the container", LABELS)
MEM = Gauge("container_memory_bytes", "Memory used by the container (excluding page cache)", LABELS)
MEM_LIMIT = Gauge("container_memory_limit_bytes", "Memory limit visible to the container", LABELS)
UPTIME = Gauge("container_uptime_seconds", "Seconds since the container started", LABELS)
RESTARTS = Gauge("container_restarts", "Container restart count", LABELS)
UP = Gauge("container_up", "1 if the container is running", LABELS)
SCRAPE_OK = Gauge("resource_exporter_last_scrape_success", "1 if the last Docker API scrape succeeded")

_prev: dict[str, tuple[int, float]] = {}


def client() -> httpx.Client:
    return httpx.Client(transport=httpx.HTTPTransport(uds=SOCKET), base_url="http://docker", timeout=10)


def own_project(c: httpx.Client) -> str | None:
    """The compose project this exporter belongs to (so only our containers are reported)."""
    try:
        me = c.get(f"/containers/{socket.gethostname()}/json").json()
        return me["Config"]["Labels"].get("com.docker.compose.project")
    except Exception:  # noqa: BLE001
        return os.environ.get("COMPOSE_PROJECT_NAME")


def parse_started(ts: str) -> float:
    ts = ts.split(".")[0].rstrip("Z")
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def scrape(c: httpx.Client, project: str | None) -> None:
    flt = {"label": [f"com.docker.compose.project={project}"]} if project else {}
    containers = c.get("/containers/json", params={"all": "true", "filters": json.dumps(flt)}).json()
    seen = set()
    for info in containers:
        name = info["Names"][0].lstrip("/")
        service = info["Labels"].get("com.docker.compose.service", name)
        labels = (service, name)
        seen.add(labels)
        running = info["State"] == "running"
        UP.labels(*labels).set(1 if running else 0)
        detail = c.get(f"/containers/{info['Id']}/json").json()
        RESTARTS.labels(*labels).set(detail.get("RestartCount", 0))
        if not running:
            CPU.labels(*labels).set(0)
            UPTIME.labels(*labels).set(0)
            continue
        UPTIME.labels(*labels).set(time.time() - parse_started(detail["State"]["StartedAt"]))
        st = c.get(f"/containers/{info['Id']}/stats", params={"stream": "false", "one-shot": "true"}).json()
        total = int(st.get("cpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0))
        now = time.time()
        if info["Id"] in _prev:
            p_total, p_time = _prev[info["Id"]]
            dt = now - p_time
            if dt > 0 and total >= p_total:
                CPU.labels(*labels).set((total - p_total) / 1e9 / dt)
        _prev[info["Id"]] = (total, now)
        mem = st.get("memory_stats", {})
        stats = mem.get("stats", {})
        cache = stats.get("inactive_file", stats.get("total_inactive_file", stats.get("cache", 0)))
        MEM.labels(*labels).set(max(0, int(mem.get("usage", 0)) - int(cache or 0)))
        if mem.get("limit"):
            MEM_LIMIT.labels(*labels).set(int(mem["limit"]))
    for g in (CPU, MEM, MEM_LIMIT, UPTIME, RESTARTS, UP):  # drop removed containers (e.g. after scaling down)
        for key in list(g._metrics):  # noqa: SLF001
            if key not in seen:
                g.remove(*key)


def loop() -> None:
    c = client()
    project = own_project(c)
    while True:
        try:
            scrape(c, project)
            SCRAPE_OK.set(1)
        except Exception:  # noqa: BLE001 - keep exporting the last values
            SCRAPE_OK.set(0)
        time.sleep(EVERY)


def main() -> None:
    start_http_server(int(os.environ.get("METRICS_PORT", "9103")))
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    t.join()


if __name__ == "__main__":
    main()
