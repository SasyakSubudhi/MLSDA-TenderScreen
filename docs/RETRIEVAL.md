# Retrieval: corpus, chunking, embeddings, index

## Corpus

| doc_id | Document | Family | Pages |
|---|---|---|---|
| `tender` | IIT Palakkad NIT IITPKD/AR/RS/053/2018-19, supply of desktop computers for research scholars (published 28-03-2019 on CPPP and the institute site, closed 22-04-2019) | tender | 20 |
| `doe_om_2016_07` | DoE OM F.20/2/2014-PPD(Pt.) 25-07-2016 - relaxation for Startups and MSEs (Ministry of Coal copy, scanned) | policy | 2 |
| `doe_om_2016_09` | DoE OM 20-09-2016 - relaxation for Startups (reprinted with the 25-07-2016 OM in the DPE compendium, pp. 209-210) | policy | 2 |
| `gfr_2017` | General Financial Rules 2017, chapter 6 (official PDF pages 40-57) | policy | 18 |

Source URLs, SHA-256 hashes and page offsets (to cite original page numbers)
are in `corpus/manifest.json`. All four have a text layer; no OCR needed. The
25-07-2016 scan has OCR noise in one paragraph, so the clean reprint of the
same paragraph in the DPE compendium is also registered as a clause
(`DOE-2016-07-REPRINT`) and noisy quotes are ranked below clean ones.

The tender was published on the Central Public Procurement Portal, as the
brief asks; clause 1.2 of the tender itself says it can be "downloaded from
https://iitpkd.ac.in/tenders or https://eprocure.gov.in/cppp/" (registry clause
`TENDER-1.2`, checked by a unit test).

## Why the corpus is difficult, and what the design does about it

The tender says "turnover of at least Rs.5 Lakh during each of the last three
financial years" and that "Bids not accompanied by the EMD shall be rejected",
and never mentions startups or MSEs. The relaxation lives in two other documents that never mention this
tender. A single similarity search for the *bid* ("startup, turnover
8.8 lakh, desktop tender") returns the tender clause and nothing from the
OMs. Three design choices fix that:

1. **Two document families.** Every chunk is labelled `tender` or `policy`,
   and `search_corpus` can be restricted to one family. The agent searches the
   tender for the condition, then deliberately searches the *policy* family
   for modifiers - the second corpus is never crowded out by tender text.
2. **Clause units with topic and category metadata.** Each curated clause
   (`corpus/clauses.json`, 20 clauses cut verbatim from the PDFs) is indexed
   as one intact passage. Its embedded text is prefixed with its title, topics
   ("prior turnover", "prior experience", "emd") and the bidder categories it
   concerns ("startup", "mse"). A category-aware query derived by the agent
   ("relaxation of prior turnover criteria for Startups in public
   procurement") therefore lands on the relaxation clause even though the
   tender never names it.
3. **Hybrid search.** BM25 catches exact terms (EMD, Rule 170, MSEs) that
   small embedding models blur; vectors catch paraphrase. Reciprocal-rank
   fusion merges them.

## Chunking

* **Clause units** - 20 curated clauses (15 tender, 5 policy), intact.
* **Page windows** - every page split by LangChain's
  `RecursiveCharacterTextSplitter` into windows of `CHUNK_SIZE`=900 characters
  with `CHUNK_OVERLAP`=150, separators sentence -> clause -> word. Windows never
  cross a page boundary, so every citation has an exact page. Windows are
  tagged with the clause ids whose opening or closing words they contain.
* Text is normalised once at ingestion (letter-spaced headings joined,
  typographic quotes and dashes unified, whitespace collapsed) so the text the
  agent quotes is exactly the text the verifier checks.

Result: 210 chunks (190 windows + 20 clause units).

## Embeddings

A small local sentence-embedding model (384 dimensions) run through
fastembed's ONNX runtime - no PyTorch, which keeps the image ~800 MB smaller
and memory low on a laptop. No model identifier is stored in the repository
(requirement D4): `EMBEDDING_MODEL` is a runtime setting, and when it is
empty the embedding library's own default small English model is used. The
model actually loaded is recorded in `index/meta.json` (`embedding_backend`)
and shown at `/v1/system`; it is downloaded once into the `index-data` volume.
If it cannot be downloaded, `EMBEDDING_FALLBACK=hash` switches to a
deterministic feature-hashing embedder (also recorded); the evaluation set
still routes every scenario correctly in that mode thanks to BM25.

## Index, re-embedding and freshness

* Vectors are cached in SQLite keyed by `sha256(embedding backend + text)`.
* FAISS (LangChain `FAISS` vector store, flat L2 index on normalised vectors)
  stores one entry per chunk id.
* The pipeline is **idempotent and incremental**: it diffs the new chunk set
  against the previous one by chunk id and text hash, deletes stale ids from
  FAISS, adds new ones, and embeds only chunks not in the vector cache. A
  second run embeds 0 chunks. `--reembed-doc gfr_2017` re-embeds only that
  document's chunks and patches the index in place - no full rebuild.
* `index/meta.json` records when the index was built and last checked, the
  document hashes and versions, chunk counts, embedding backend, and the run's
  throughput (pages/s, chunks/s, seconds spent parsing and embedding,
  cache hits). Workers export them as Prometheus gauges (`index_chunks`,
  `index_built_timestamp_seconds`, `ingest_chunks_per_second`,
  `ingest_chunks_embedded_last_run`) and the API shows them at `/v1/system`.
* Workers reload automatically when `meta.json` changes.

```bash
docker compose run --rm indexer                                   # incremental
docker compose run --rm indexer python -m tenderscreen.retrieval.ingest --reembed-doc gfr_2017
docker compose run --rm indexer python -m tenderscreen.retrieval.ingest --force   # full rebuild
docker compose run --rm indexer python -m tenderscreen.retrieval.ingest --check   # freshness
```

## Caching (D7)

| Cache | Key | Why it is safe |
|---|---|---|
| embedding vectors | backend + chunk text | a vector depends only on its text |
| retrieval results (Redis, 1 h) | index version + query + family + mode + k | the index version changes whenever the index changes |
| model responses (gateway, Redis, 24 h) | model + full prompt messages | temperature 0; identical prompt -> identical answer |
| whole runs (API, 1 h) | full bid + prompt version + index version | only verified, non-degraded results are cached |
