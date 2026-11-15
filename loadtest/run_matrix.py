"""Run the Locust matrix: research-worker replicas x concurrent users.

    python loadtest/run_matrix.py                         # default matrix
    python loadtest/run_matrix.py --workers 1 3 --users 1 5 10 20 40 --duration 90s

Before running, put these lines in .env (load-test profile) so the per-client
rate limiter and the LLM response cache do not distort the measurement:
    RATE_LIMIT_PER_MIN=0
    LLM_CACHE_ENABLED=false
then `docker compose up -d` again. Locust itself runs in the `locust`
container on the compose network; this script only drives docker compose and
collects the CSV output into loadtest/results/matrix.json and matrix.md.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")
ROOT = os.path.dirname(HERE)
os.chdir(ROOT)  # docker compose and .env are resolved from the project root


def sh(cmd: list[str], check=True) -> str:
    return subprocess.run(cmd, check=check, capture_output=True, text=True).stdout


def compose(*args: str, extra: list[str] | None = None) -> list[str]:
    files = []
    for f in extra or []:
        files += ["-f", f]
    return ["docker", "compose", *files, *args]


def sample_stats(stop: threading.Event, out: dict) -> None:
    """Peak CPU% / memory per container while the test runs."""
    while not stop.is_set():
        raw = sh(["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"], check=False)
        for line in raw.strip().splitlines():
            parts = line.split("\t")
            if len(parts) < 3 or "tenderscreen" not in parts[0]:
                continue
            name = parts[0].replace("tenderscreen-", "")
            cpu = float(parts[1].rstrip("%") or 0)
            cur = out.setdefault(name, {"cpu_peak_pct": 0.0, "mem": parts[2].split(" /")[0]})
            if cpu > cur["cpu_peak_pct"]:
                cur["cpu_peak_pct"] = cpu
                cur["mem"] = parts[2].split(" /")[0]
        stop.wait(3)


def read_e2e(prefix: str) -> dict:
    with open(prefix + "_stats.csv", newline="") as fh:
        for row in csv.DictReader(fh):
            if row["Type"] == "E2E":
                return {"requests": int(row["Request Count"]), "failures": int(row["Failure Count"]),
                        "p50_s": float(row["50%"]) / 1000, "p95_s": float(row["95%"]) / 1000,
                        "max_s": float(row["Max Response Time"]) / 1000, "rps": float(row["Requests/s"])}
    return {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", nargs="+", type=int, default=[1, 3])
    ap.add_argument("--users", nargs="+", type=int, default=[1, 5, 10, 20, 40])
    ap.add_argument("--duration", default="90s")
    ap.add_argument("--slo-p95-s", type=float, default=30.0)
    ap.add_argument("--compose-file", action="append", default=[], help="extra compose file(s)")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    os.makedirs(RESULTS, exist_ok=True)
    rows = []
    for w in args.workers:
        sh(compose("up", "-d", "--scale", f"agent-worker={w}", extra=args.compose_file))
        time.sleep(15)  # replicas register and load the index
        for u in args.users:
            name = f"{args.tag}w{w}_u{u}"
            stop, stats = threading.Event(), {}
            t = threading.Thread(target=sample_stats, args=(stop, stats), daemon=True)
            t.start()
            print(f"running {name} ...", flush=True)
            sh(compose("--profile", "loadtest", "run", "--rm", "locust", "--headless", "-u", str(u), "-r",
                       str(max(1, u // 5)), "-t", args.duration, "--stop-timeout", "120", "--host",
                       "http://api:8000", "--csv", f"/mnt/locust/results/{name}", "--only-summary",
                       extra=args.compose_file), check=False)
            stop.set()
            t.join()
            e2e = read_e2e(os.path.join(RESULTS, name))
            row = {"workers": w, "users": u, **e2e, "slo_met": bool(e2e) and e2e["p95_s"] <= args.slo_p95_s
                   and e2e["failures"] == 0, "containers": stats}
            rows.append(row)
            print(json.dumps({k: v for k, v in row.items() if k != "containers"}), flush=True)
    with open(os.path.join(RESULTS, f"{args.tag}matrix.json"), "w") as fh:
        json.dump(rows, fh, indent=1)
    lines = ["| research workers | users | completed runs | failures | runs/s | p50 (s) | p95 (s) | SLO p95<=30s |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['workers']} | {r['users']} | {r.get('requests')} | {r.get('failures')} | "
                     f"{r.get('rps', 0):.2f} | {r.get('p50_s', 0):.1f} | {r.get('p95_s', 0):.1f} | "
                     f"{'met' if r['slo_met'] else 'NOT met'} |")
    with open(os.path.join(RESULTS, f"{args.tag}matrix.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
