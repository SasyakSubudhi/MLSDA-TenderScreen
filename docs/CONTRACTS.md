# Data contracts for every external interface

Machine-readable schemas: [contracts/bid_summary.schema.json](../contracts/bid_summary.schema.json),
[contracts/feedback.schema.json](../contracts/feedback.schema.json); live at
`GET /v1/contract` and in the OpenAPI document at `/docs`.

## 1. `POST /v1/screenings` - submit a bid summary (version 1.0)

| Field | Type / allowed values | Rule |
|---|---|---|
| `schema_version` | `"1.0"` | categorical |
| `bid_id` | string `^[A-Za-z0-9._-]{3,40}$` | pattern |
| `tender_id` | `"IITPKD/AR/RS/053/2018-19"` | categorical (supported tenders) |
| `bidder.legal_name` | string 2-200, **free text** | injection + toxicity scan |
| `bidder.declared_category` | `large_enterprise` `mse` `startup` `consortium` `foreign` `not_stated` | categorical |
| `bidder.country_of_incorporation` | ISO-2 `^[A-Z]{2}$` | pattern |
| `bidder.date_of_incorporation` | date | cross-field (below) |
| `bidder.is_oem` | bool (true if the bidder is the OEM) | - |
| `bidder.registered_office_state` | one of the 36 states/UTs or `Outside India` | categorical |
| `bidder.service_centre_states` | list of states/UTs, max 36 | categorical |
| `bidder.udyam_registration` | `^UDYAM-[A-Z]{2}-\d{2}-\d{7}$` or null | pattern |
| `bidder.nsic_registration` | `^[A-Za-z0-9/-]{4,40}$` or null | pattern, scanned |
| `bidder.dpiit_recognition` | `^DIPP\d{3,8}$` or null | pattern |
| `bidder.consortium_members` | list of strings, max 10, **free text** | scanned |
| `financials.annual_turnover_inr` | object FY -> number | exactly 2015-16, 2016-17, 2017-18, each 0 .. 1e13 |
| `experience.years_in_desktop_business` | int 0 .. 100 | range, cross-field |
| `experience.cfti_supply_orders` | int 0 .. 10000 | range |
| `emd.status` | `paid` `exemption_claimed` `not_paid` | categorical |
| `emd.amount_inr` | number 0 .. 1e9 | range |
| `emd.transaction_ref` | `^[A-Za-z0-9-]{4,40}$` or null | pattern, scanned |
| `documents.*` | 11 booleans (audited statements, CA certificate, purchase orders, performance certificate, incorporation certificate, OEM authorisation, signed tender copy, compliance statement, page declaration, MSME certificate, startup recognition certificate) | unknown keys rejected |
| `remarks` | string <= 2000 or null, **free text, untrusted** | injection + toxicity scan |

Unknown fields anywhere are rejected (`extra=forbid`).

**Cross-field rules**

1. Turnover has exactly the three financial years 2015-16, 2016-17, 2017-18.
2. Incorporation date between 1850-01-01 and the tender closing date 22-04-2019.
3. Years in the desktop business cannot exceed the bidder's age at closing (rounded up).
4. `startup` requires `dpiit_recognition`.
5. `mse` requires `udyam_registration` or `nsic_registration`.
6. `consortium` requires at least two members; other categories none.
7. `foreign` if and only if the country is not `IN`.
8. `registered_office_state = "Outside India"` if and only if the country is not `IN`.
9. `emd.status = paid` requires `amount_inr > 0` and `transaction_ref`; other statuses `amount_inr = 0`.

**Responses**

| Code | Meaning | Body |
|---|---|---|
| 202 | accepted and queued | `{run_id, status: "queued", status_url, trace_url, poll_after_ms}` |
| 200 | identical bid already screened (whole-run cache) | `{run_id, status: "completed", cached: true, ...}` |
| 422 | rejected at the boundary and **quarantined**; **no agent run started** | `{error, reason, quarantine_id, agent_run_started: false, errors: [{field, error, message}]}` |
| 429 | per-client rate limit (`X-Client-Id` header, else IP) | `{error: "rate_limited", retry_after_s}` |

Optional query parameters: `prompt_version=v1|v2`; `demo_fault=unsupported_citation` (only when `DEMO_MODE=true`).

## 2. `GET /v1/screenings/{run_id}` - poll

`{run_id, status, created_at, elapsed_s, attempt, result?, overdue?}`.
`status` is `queued`, `researching`, `verifying`, `retrying`, `completed` or `failed`.
`result`:

| Field | Meaning |
|---|---|
| `route` | `pass_to_technical_evaluation` / `seek_clarification` / `reject_with_cited_condition` / `escalate_to_human` |
| `route_label`, `summary`, `basis_conditions` | human-readable route and the conditions it rests on |
| `findings[]` | `{condition_id, title, status, justification, values, citations[]}` |
| `citations[]` | `{evidence_id, chunk_id, source, title, page, original_page, url, quote, role}` - every quote is verbatim retrieved text |
| `escalation` | `{to, reasons[], citations[], next_steps[]}` or null |
| `verification` | `{passed, decision, attempt, checks[]}` |
| `verified`, `degraded`, `incomplete`, `provisional` | trust flags |
| `prompt_version`, `stats`, `e2e_seconds` | provenance |
| `disclaimer` | "This is a screening finding ... the Tender Committee decides." |

## 3. `GET /v1/screenings/{run_id}/trace`

`{run, bid, summary, events[]}`. Event types: `submitted`, `research_started`,
`plan_decision`, `tool_call` (args, passages, latency), `llm_call` (task,
prompt version, tokens, cost, latency, response), `reflect`, `assessment`,
`handoff`, `verification` (all checks), `retry`, `redelivered`, `final`,
`live_quality_sample`, `feedback`.

## 4. `POST /v1/screenings/{run_id}/feedback`

`{satisfactory: bool, reason: string 3..500}` -> `{stored: true, ...}`.
Aggregated at `GET /v1/feedback/summary`.

## 5. Internal interfaces

| Interface | Contract |
|---|---|
| `stream:research` | `{run_id, attempt, strategy: "standard"|"keyword_lookup", prior_failures[], trace_carrier}` |
| `stream:verify` | `{run_id, handoff: {bid, profile, evidence{chunk_id: passage}, draft, attempt, prompt_version, degraded, incomplete, stats}, trace_carrier}` |
| `POST llm-gateway:8100/v1/complete` | `{task, messages[], max_tokens, temperature, run_id, agent, step, prompt_version}` -> `{content, usage{prompt_tokens, completion_tokens}, cost_usd, cached, provider, latency_ms}`; 503 `{error: circuit_open | provider_unavailable | daily_budget_exhausted}` |
| provider | OpenAI-compatible `POST {LLM_BASE_URL}/chat/completions` |
