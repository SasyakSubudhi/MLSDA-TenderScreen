"""Measure the effect of the LLM response cache on p95 and cost per run (D7).

Runs the same Locust load twice - LLM_CACHE_ENABLED=false, then true - and
reads cost and completed runs from Prometheus. Bids have randomised turnover
figures (as in the main load test), so only prompts that are genuinely
identical across bids (planning, query derivation for the same category) can
be served from cache; the drafting prompt always differs.

    python loadtest/cache_experiment.py --workers 3 --users 10 --duration 90s
"""
import argparse
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request

from run_matrix import RESULTS, compose, read_e2e, sh  # (importing it also chdirs to the project root)

PROM = os.environ.get("PROM_URL", "http://localhost:9090")


def prom(q: str) -> float:
    url = f"{PROM}/api/v1/query?query={urllib.parse.quote(q)}"
    data = json.loads(urllib.request.urlopen(url, timeout=10).read())["data"]["result"]
    return float(data[0]["value"][1]) if data else 0.0


def set_env(key: str, value: str) -> None:
    lines = open(".env").read().splitlines() if os.path.exists(".env") else []
    lines = [l for l in lines if not l.startswith(key + "=")] + [f"{key}={value}"]
    open(".env", "w").write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--users", type=int, default=10)
    ap.add_argument("--duration", default="90s")
    ap.add_argument("--compose-file", action="append", default=[])
    a = ap.parse_args()
    rows = []
    for enabled in ("false", "true"):
        set_env("LLM_CACHE_ENABLED", enabled)
        sh(compose("up", "-d", "--scale", f"agent-worker={a.workers}", extra=a.compose_file))
        sh(compose("exec", "-T", "redis", "sh", "-c",
                   "redis-cli --scan --pattern 'llm:cache:*' | xargs -r redis-cli del", extra=a.compose_file), check=False)
        time.sleep(20)
        c0, r0 = prom("sum(llm_cost_usd_total)"), prom("sum(runs_finalized_total)")
        h0, m0 = prom('sum(llm_cache_total{result="hit"})'), prom("sum(llm_cache_total)")
        name = f"cache_{enabled}"
        sh(compose("--profile", "loadtest", "run", "--rm", "locust", "--headless", "-u", str(a.users), "-r", "2",
                   "-t", a.duration, "--stop-timeout", "120", "--host", "http://api:8000",
                   "--csv", f"/mnt/locust/results/{name}", "--only-summary", extra=a.compose_file), check=False)
        time.sleep(8)  # let Prometheus scrape the final counters
        runs = prom("sum(runs_finalized_total)") - r0
        cost = prom("sum(llm_cost_usd_total)") - c0
        hits = prom('sum(llm_cache_total{result="hit"})') - h0
        lookups = prom("sum(llm_cache_total)") - m0
        e2e = read_e2e(os.path.join(RESULTS, name))
        rows.append({"llm_cache": enabled == "true", **e2e, "runs_counted": runs,
                     "cost_per_run_usd": round(cost / runs, 8) if runs else None,
                     "cost_per_100_runs_usd": round(cost / runs * 100, 6) if runs else None,
                     "cache_hit_ratio": round(hits / lookups, 3) if lookups else 0.0})
        print(json.dumps(rows[-1]), flush=True)
    set_env("LLM_CACHE_ENABLED", "false")
    with open(os.path.join(RESULTS, "cache_experiment.json"), "w") as fh:
        json.dump(rows, fh, indent=1)


if __name__ == "__main__":
    main()
