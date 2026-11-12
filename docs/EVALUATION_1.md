# Evaluation 1 - Corpus confirmation and project plan

**Mini Project 06 - Public Procurement Bid Compliance Screening Service**
Team: PES1UG24AM256 · PES1UG24AM266 · PES1UG24AM269 · PES1UG24AM292

## 1. Corpus confirmation

All four documents are official, free to download, have a text layer (no OCR
needed) and are stored with their SHA-256 hash in
[corpus/manifest.json](../corpus/manifest.json).

| # | Document required by the brief | What we use | Source |
|---|---|---|---|
| 1 | One real published tender with eligibility criteria | IIT Palakkad NIT **IITPKD/AR/RS/053/2018-19** - supply, installation, testing and commissioning of desktop computers for research scholars. Published 28-03-2019, **closed 22-04-2019** (a closed tender, as section 13 requires). Clause 1.2 states it was published on the **Central Public Procurement Portal**: "downloaded from https://iitpkd.ac.in/tenders or https://eprocure.gov.in/cppp/" | https://smail.iitpkd.ac.in/sites/default/files/2019-03/NIT_DESKTOPS_RS_1.pdf |
| 2 | DoE orders on relaxation of prior turnover / prior experience for startups and MSEs | DoE OM No. F.20/2/2014-PPD(Pt.) dated **25-07-2016** (Ministry of Coal copy) and DoE OM of even number dated **20-09-2016** (reproduced, with the 25-07-2016 OM, in the DPE compendium, pp. 209-210) | https://coal.nic.in/sites/default/files/2020-01/02-08-16-a.pdf ; https://www.dpe.gov.in/static/uploads/2025/07/89f7e04fda067d5652584449f1e4ee18.pdf |
| 3 | GFR 2017 chapters covering procurement | General Financial Rules 2017, Chapter 6 "Procurement of Goods and Services" (official PDF pages 40-57) | https://cga.nic.in/writereaddata/file/GFR2017.pdf |

### Eligibility criteria of the tender (the mandatory conditions we screen)

| Condition | Tender clause | Requirement |
|---|---|---|
| C1 Turnover | Annexure-II 7; clause 5.9(ii) | at least Rs.5 Lakh in **each** of 2015-16, 2016-17, 2017-18; audited statements certified by a CA |
| C2 Experience | Annexure-II 6 and 8; clause 5.9(i) | at least three financial years in the desktop-computer business; desktops supplied to Centrally Funded Technical Institutions in the last five years, with purchase orders and a performance certificate |
| C3 EMD | clause 5.3, 5.3.1; Annexure-VIII | EMD @ 2% of the estimated value = **Rs.17,600**; "Bids not accompanied by the EMD shall be rejected/disqualified" |
| C4 Documents | 5.9, 5.10, Annexure-II 1, 2, 10, Annexure-VII | audited statements, CA certification, purchase orders, performance certificate, registration certificate, OEM authorisation (if not the OEM), signed tender copy, compliance sheet, page declaration |
| C5 Regional presence | Annexure-II 4-5 | registered office **and** service centre in Karnataka / Tamil Nadu / Telangana / Andhra Pradesh / Kerala |

## 2. The passages the section 4.1 case depends on

**Case:** "A recognised startup has bid. Its annual turnover is below the
threshold stated in the tender's eligibility criteria."

**Tender (says: not met)** - Annexure-II item 7, page 13:
> "7.The bidder must provide proof of turnover of at least Rs.5 Lakh during
> each of the last three financial years (2015-16, 2016-17 and 2017-18)."

(the same requirement is in clause 5.9(ii), page 4). The tender does not
mention startups anywhere.

**Central policy (says: relax it)**

* DoE OM 20-09-2016, para 2 (DPE compendium p. 209, PDF page 235):
  > "...it is again clarified that normally for all public procurement, the
  > Central Ministries/Departments have to ensure that criteria of prior
  > turnover and prior experience for all Startups is relaxed subject to their
  > meeting of quality and technical specification."
* GFR 2017 Rule 173(i) (official PDF page 49):
  > "The condition of prior turnover and prior experience may be relaxed for
  > Startups (as defined by Department of Industrial Policy and Promotion)
  > subject to meeting of quality & technical specifications and making
  > suitable provisions in the bidding document."
* DoE OM 25-07-2016, para 3 (cross-referenced by the 20-09-2016 OM): all
  Central Ministries/Departments may relax prior turnover and prior experience
  for all Startups (whether MSEs or otherwise).

Neither side cross-references the other. Read alone, the tender says reject;
the policy says relax, subject to conditions (quality/technical
specifications, and GFR 173 adds "suitable provisions in the bidding
document", which this tender does not make).

**Correct behaviour (reference answer):** escalate to the Tender Committee
with both the tender clause and the policy provisions cited - not a decision.

### Values we test with

[examples/startup_low_turnover.json](../examples/startup_low_turnover.json),
evaluation scenario **S02**:

| Field | Value |
|---|---|
| declared category | startup, DPIIT recognition `DIPP12345`, recognition certificate enclosed |
| incorporated | 12-05-2015 (3.9 years before closing) |
| turnover 2015-16 / 2016-17 / 2017-18 | Rs.1,50,000 / Rs.3,20,000 / Rs.4,60,000 - below Rs.5,00,000 in every year |
| years in desktop business / CFTI orders | 3 / 2 (experience met) |
| EMD | Rs.17,600 paid |
| documents, regional presence | all enclosed; office Kerala, service centres Kerala and Tamil Nadu |

**Expected output:** route `escalate_to_human`; C1 turnover = `CONFLICT`
citing Annexure-II 7 **and** DoE OM 20-09-2016 / GFR 173 (and the 25-07-2016
OM); every other condition `MET`.

A second tender-vs-rules conflict is also in the set (S11): an MSE claims EMD
exemption; the tender demands EMD from every bidder (5.3.1) while GFR 2017
Rule 170(i) obtains bid security "from the bidders except Micro and Small
Enterprises". Expected: escalate with both cited.

## 3. Project plan

**Approach (from section 10 of the brief):** a working end-to-end path first,
then requirements in order of marks at risk; the 4.1 case and the
demonstration items drive the priorities.

| Week | Milestone | Requirements |
|---|---|---|
| 1 | Corpus confirmed (this document); data contract; Compose skeleton; ingestion pipeline and index; stub model | F1, F2, D1, D2, D3, L1 |
| 2 | **End-to-end path working**: API -> queue -> research agent with tools -> verifier -> result; web client | F3-F6, A1-A4, A8 |
| 3 | Escalation and verification hardening (4.1 case, cross-references, retries); guardrails; reliability (timeouts, retries, breaker, fallback) | F7, A3, A5, L4 |
| 4 | Observability (structured logs, traces, metrics, dashboard); cost tracking; caching and rate limiting | F8, A6, D5, L5, D7 |
| 5 | Evaluation set (28 scenarios), prompt variants, deliberate change, TruLens; feedback endpoint | L2, L3, L6 |
| 6 | SLO declared, Locust load tests with the stub, `--scale` measurement; runbook, incident log; report; demo rehearsal from a clean clone | D6, A7, D8, deliverables |

### Division of work

| Part | Member (GitHub) | Scope |
|---|---|---|
| 1 Platform, API, contracts | SasyakSubudhi | Compose/Dockerfile/config, shared runtime, FastAPI service, data contract and quarantine, rate limiting, run cache, web client |
| 2 Corpus, retrieval, evaluation | vasun26shriya | corpus and clause registry, ingestion pipeline, embeddings cache, FAISS+BM25 search, evaluation set, metrics, TruLens |
| 3 Agents and guardrails | sujan1001 | research agent, verifier agent, tools, rule engine, guardrails, versioned prompts, queue workers |
| 4 LLM ops, observability, load | yasho-26singh | LLM gateway (breaker, retries, cache, budget, cost), stub provider, resource exporter, Prometheus/Grafana, Locust, runbook, incidents, report |

### Main risks and mitigations

| Risk | Mitigation |
|---|---|
| Free-tier model rate limits during demo / load test | stub provider for load tests (D6); response cache; per-client rate limit; local Ollama profile as a keyless real model |
| Retrieval misses the policy relaxation (the corpus difficulty) | two document families, clause units with topic/category metadata, hybrid search, agent derives policy queries after reading the tender clause |
| Wrong confident answer | rule engine computes statuses from retrieved text; independent verifier; escalation when sources conflict or evidence is missing |
| System does not start at the demo (zero marks) | single `docker compose up`, health checks, clean-clone rehearsal, images and model cached before the demo |
