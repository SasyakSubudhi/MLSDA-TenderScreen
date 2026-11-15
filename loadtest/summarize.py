"""Summarise one Locust run (used by the Windows load-test script).

Runs inside the `locust` container (it has Python and is on the compose
network), right after a run finishes:

    python /mnt/locust/summarize.py matrix <name> <workers> <users>
    python /mnt/locust/summarize.py cache  <name> <true|false>
    python /mnt/locust/summarize.py table

Reads results/<name>_stats.csv, asks Prometheus for the peak CPU of every
container during the run window (from the resource-exporter) and - for the
cache experiment - the cost and runs completed in the window, and appends a
row to results/matrix.json or results/cache_experiment.json. `table` writes
results/matrix.md and results/cache_experiment.md.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import urllib.parse
import urllib.request

RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
PROM = os.environ.get("PROM_URL", "http://prometheus:9090")
SLO_P95_S = 30.0
WINDOW = "4m"  # 90 s run + drain + scrape lag


def prom(query: str) -> list:
    url = f"{PROM}/api/v1/query?query={urllib.parse.quote(query)}"
    try:
        return json.loads(urllib.request.urlopen(url, timeout=10).read())["data"]["result"]
    except Exception:  # noqa: BLE001
        return []


def scalar(query: str) -> float:
    r = prom(query)
    return float(r[0]["value"][1]) if r else 0.0


def e2e(name: str) -> dict:
    with open(os.path.join(RESULTS, name + "_stats.csv"), newline="") as fh:
        for row in csv.DictReader(fh):
            if row["Type"] == "E2E":
                return {"requests": int(row["Request Count"]), "failures": int(row["Failure Count"]),
                        "p50_s": float(row["50%"]) / 1000, "p95_s": float(row["95%"]) / 1000,
                        "max_s": float(row["Max Response Time"]) / 1000, "rps": float(row["Requests/s"])}
    return {"requests": 0, "failures": 0, "p50_s": 0, "p95_s": 0, "max_s": 0, "rps": 0}


def append(file: str, row: dict) -> None:
    path = os.path.join(RESULTS, file)
    rows = json.load(open(path)) if os.path.exists(path) else []
    rows = [r for r in rows if r.get("name") != row["name"]] + [row]
    json.dump(rows, open(path, "w"), indent=1)
    print(json.dumps({k: v for k, v in row.items() if k != "containers_peak_cpu_cores"}))


def peak_cpu() -> dict:
    return {r["metric"].get("container", "?"): round(float(r["value"][1]), 3)
            for r in prom(f"max_over_time(container_cpu_cores[{WINDOW}])")}


def main() -> None:
    mode = sys.argv[1]
    if mode == "matrix":
        name, w, u = sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
        row = {"name": name, "workers": w, "users": u, **e2e(name)}
        row["slo_met"] = row["requests"] > 0 and row["p95_s"] <= SLO_P95_S and row["failures"] == 0
        row["containers_peak_cpu_cores"] = peak_cpu()
        append("matrix.json", row)
    elif mode == "cache":
        name, enabled = sys.argv[2], sys.argv[3] == "true"
        runs = scalar(f"sum(increase(runs_finalized_total[{WINDOW}]))")
        cost = scalar(f"sum(increase(llm_cost_usd_total[{WINDOW}]))")
        hits = scalar(f'sum(increase(llm_cache_total{{result="hit"}}[{WINDOW}]))')
        looks = scalar(f"sum(increase(llm_cache_total[{WINDOW}]))")
        row = {"name": name, "llm_cache": enabled, **e2e(name), "runs_counted": round(runs, 1),
               "cost_per_100_runs_usd": round(cost / runs * 100, 6) if runs else None,
               "cache_hit_ratio": round(hits / looks, 3) if looks else 0.0}
        append("cache_experiment.json", row)
    elif mode == "table":
        path = os.path.join(RESULTS, "matrix.json")
        if os.path.exists(path):
            lines = ["| research workers | users | completed runs | failures | runs/s | p50 (s) | p95 (s) | SLO p95<=30s |",
                     "|---|---|---|---|---|---|---|---|"]
            for r in sorted(json.load(open(path)), key=lambda r: (r["workers"], r["users"])):
                lines.append(f"| {r['workers']} | {r['users']} | {r['requests']} | {r['failures']} | {r['rps']:.2f} | "
                             f"{r['p50_s']:.1f} | {r['p95_s']:.1f} | {'met' if r['slo_met'] else 'NOT met'} |")
            open(os.path.join(RESULTS, "matrix.md"), "w").write("\n".join(lines) + "\n")
            print("\n".join(lines))
        path = os.path.join(RESULTS, "cache_experiment.json")
        if os.path.exists(path):
            lines = ["| LLM response cache | completed runs | runs/s | p50 (s) | p95 (s) | cache hit ratio | cost per 100 runs (USD) |",
                     "|---|---|---|---|---|---|---|"]
            for r in json.load(open(path)):
                lines.append(f"| {'on' if r['llm_cache'] else 'off'} | {r['requests']} | {r['rps']:.2f} | {r['p50_s']:.1f} | "
                             f"{r['p95_s']:.1f} | {r['cache_hit_ratio']:.0%} | {r['cost_per_100_runs_usd']} |")
            open(os.path.join(RESULTS, "cache_experiment.md"), "w").write("\n".join(lines) + "\n")
            print("\n".join(lines))


if __name__ == "__main__":
    main()
