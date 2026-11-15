"""Generate observability/grafana/dashboards/tenderscreen.json.

Run:  python observability/build_dashboard.py
Panels cover requirement D5 (p50/p95 latency, error rate, throughput,
availability, CPU/memory per container) and A6 (per-agent success rate,
steps and tool calls per run, end-to-end and per-step latency, retry rate,
container uptime), plus cost, cache, quality-over-time and index freshness.
"""
import json
import os

DS = {"type": "prometheus", "uid": "prometheus"}
panels = []
_y = [0]


def panel(title, exprs, kind="timeseries", unit="short", w=12, h=8, legend=True, desc=""):
    x = 0 if not panels or panels[-1]["gridPos"]["x"] + panels[-1]["gridPos"]["w"] + w > 24 else \
        panels[-1]["gridPos"]["x"] + panels[-1]["gridPos"]["w"]
    if x == 0 and panels:
        _y[0] = max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in panels)
    p = {
        "id": len(panels) + 1, "type": kind, "title": title, "description": desc, "datasource": DS,
        "gridPos": {"x": x, "y": _y[0], "w": w, "h": h},
        "targets": [{"refId": chr(65 + i), "datasource": DS, "expr": e, "legendFormat": lf}
                    for i, (e, lf) in enumerate(exprs)],
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "options": {"legend": {"showLegend": legend, "displayMode": "list", "placement": "bottom"},
                    "tooltip": {"mode": "multi"}},
    }
    if kind == "stat":
        p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value", "graphMode": "area"}
    panels.append(p)


def row(title):
    _y[0] = max((p["gridPos"]["y"] + p["gridPos"]["h"] for p in panels), default=0)
    panels.append({"id": len(panels) + 1, "type": "row", "title": title, "collapsed": False,
                   "gridPos": {"x": 0, "y": _y[0], "w": 24, "h": 1}, "panels": []})
    _y[0] += 1


row("Service level - complete runs (submission to final result)")
panel("Run latency p50 / p95 (E2E)", [
    ('histogram_quantile(0.50, sum by (le) (rate(run_e2e_seconds_bucket[2m])))', "p50"),
    ('histogram_quantile(0.95, sum by (le) (rate(run_e2e_seconds_bucket[2m])))', "p95"),
    ('vector(30)', "SLO target p95 = 30 s")], unit="s", w=8)
panel("Throughput - completed runs / s by route", [
    ('sum by (route) (rate(runs_finalized_total[1m]))', "{{route}}")], unit="reqps", w=8)
panel("Error rate", [
    ('(sum(rate(http_requests_total{status=~"5.."}[2m])) or vector(0)) / clamp_min(sum(rate(http_requests_total[2m])), 1e-9)', "API 5xx ratio"),
    ('(sum(rate(runs_finalized_total{verified="false"}[2m])) or vector(0)) / clamp_min(sum(rate(runs_finalized_total[2m])), 1e-9)', "unverified/failed runs ratio"),
    ('(sum(rate(llm_requests_total{outcome!~"ok|cache_hit"}[2m])) or vector(0)) / clamp_min(sum(rate(llm_requests_total[2m])), 1e-9)', "LLM call failure ratio")],
    unit="percentunit", w=8)
panel("Availability (5 min): scrapes and running containers", [
    ('avg_over_time(up[5m])', "scrape {{job}} {{instance}}"),
    ('avg_over_time(container_up[5m])', "running {{container}}")], unit="percentunit", w=8)
panel("API request latency p95 by route", [
    ('histogram_quantile(0.95, sum by (le, route) (rate(http_request_seconds_bucket[2m])))', "{{route}}")], unit="s", w=8)
panel("Queue depth", [('max by (stream) (api_queue_depth)', "{{stream}}")], w=8)

row("Agents (A6)")
panel("Task success rate per agent", [
    ('sum by (agent) (rate(agent_tasks_total{outcome=~"ok|accept"}[5m])) / clamp_min(sum by (agent) (rate(agent_tasks_total[5m])), 1e-9)',
     "{{agent}}")], unit="percentunit", w=8)
panel("Steps and tool calls per run", [
    ('sum by (agent) (rate(agent_steps_per_run_sum[5m])) / clamp_min(sum by (agent) (rate(agent_steps_per_run_count[5m])), 1e-9)', "steps {{agent}}"),
    ('sum by (agent) (rate(agent_tool_calls_per_run_sum[5m])) / clamp_min(sum by (agent) (rate(agent_tool_calls_per_run_count[5m])), 1e-9)', "tool calls {{agent}}"),
    ('sum by (agent) (rate(agent_llm_calls_per_run_sum[5m])) / clamp_min(sum by (agent) (rate(agent_llm_calls_per_run_count[5m])), 1e-9)', "LLM calls {{agent}}")],
    w=8)
panel("Agent time per run p95", [
    ('histogram_quantile(0.95, sum by (le, agent) (rate(agent_run_seconds_bucket[5m])))', "{{agent}}")], unit="s", w=8)
panel("Per-step latency p95", [
    ('histogram_quantile(0.95, sum by (le, agent, step) (rate(agent_step_seconds_bucket[5m])))', "{{agent}} {{step}}")],
    unit="s", w=12)
panel("Retry rate", [
    ('sum by (agent, kind) (rate(agent_retries_total[5m])) or label_replace(vector(0), "kind", "none", "", "")', "{{agent}} {{kind}}"),
    ('sum(rate(llm_retries_total[5m]))', "gateway provider retries"),
    ('sum by (agent, what) (rate(agent_fallbacks_total[5m]))', "fallback {{agent}} {{what}}")], unit="ops", w=12)
panel("Container uptime (all containers)", [('container_uptime_seconds', "{{container}}")], unit="s", w=12)
panel("Verification checks failing", [
    ('sum by (check) (increase(verification_checks_total{outcome="fail"}[10m])) or label_replace(vector(0), "check", "none failing", "", "")', "{{check}}")], w=12)

row("Resources per container (every container, from the Docker API)")
panel("CPU per container (cores)", [('container_cpu_cores', "{{container}}")], w=12)
panel("Memory per container", [('container_memory_bytes', "{{container}}")], unit="bytes", w=12)
panel("Container up (1 = running)", [('container_up', "{{container}}")], w=12)
panel("Container restarts", [('container_restarts', "{{container}}")], w=12)

row("Model provider, cost and caches (L5, D7, A5)")
panel("Circuit breaker state (0 closed, 1 half-open, 2 open)", [('llm_breaker_state', "breaker")], w=8)
panel("Cost (list price) USD / min by task", [('sum by (task) (rate(llm_cost_usd_total[5m])) * 60', "{{task}}")],
      unit="currencyUSD", w=8)
panel("Tokens / min", [('sum by (type) (rate(llm_tokens_total[5m])) * 60', "{{type}}")], w=8)
panel("Cache hit ratio", [
    ('sum(rate(llm_cache_total{result="hit"}[5m])) / clamp_min(sum(rate(llm_cache_total[5m])), 1e-9)', "LLM responses"),
    ('sum(rate(retrieval_cache_total{result="hit"}[5m])) / clamp_min(sum(rate(retrieval_cache_total[5m])), 1e-9)', "retrieval"),
    ('(sum(rate(run_cache_total{result="hit"}[5m])) or vector(0)) / clamp_min(sum(rate(run_cache_total[5m])), 1e-9)', "whole runs")],
    unit="percentunit", w=8)
panel("Ingestion boundary: quarantined / rate limited", [
    ('sum by (reason) (increase(requests_quarantined_total[10m])) or label_replace(vector(0), "reason", "none", "", "")', "quarantined {{reason}}"),
    ('sum(increase(requests_rate_limited_total[10m])) or vector(0)', "rate limited")], w=8)
panel("Provider calls by outcome", [('sum by (outcome) (rate(llm_requests_total[2m]))', "{{outcome}}")], unit="ops", w=8)

row("Quality and data freshness (L6, D2)")
panel("Live quality score (sampled runs)", [('max by (metric) (live_quality_score)', "{{metric}}")],
      unit="percentunit", w=12)
panel("Feedback", [('sum by (satisfactory) (feedback_total) or label_replace(vector(0), "satisfactory", "none yet", "", "")', "satisfactory={{satisfactory}}")], w=6)
panel("Index age", [('time() - max(index_built_timestamp_seconds)', "age")], kind="stat", unit="s", w=3)
panel("Index chunks", [('max(index_chunks)', "chunks")], kind="stat", w=3)
panel("Ingestion throughput (chunks/s, last run)", [('max(ingest_chunks_per_second)', "chunks/s")], kind="stat", w=6)
panel("Chunks embedded in last ingestion", [('max(ingest_chunks_embedded_last_run)', "embedded")], kind="stat", w=6)

dash = {
    "uid": "tenderscreen", "title": "TenderScreen - bid screening service", "schemaVersion": 39, "version": 1,
    "refresh": "5s", "time": {"from": "now-30m", "to": "now"}, "tags": ["tenderscreen"], "timezone": "browser",
    "panels": panels, "templating": {"list": []}, "annotations": {"list": []},
}
out = os.path.join(os.path.dirname(__file__), "grafana", "dashboards", "tenderscreen.json")
with open(out, "w") as fh:
    json.dump(dash, fh, indent=1)
print(f"wrote {out} with {len(panels)} panels")
