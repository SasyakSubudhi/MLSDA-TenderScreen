"""Part 2 - corpus, ingestion pipeline, retrieval, evaluation set."""
import json
import os

from tenderscreen.retrieval.normalize import canon, normalize_text, sentences
from tenderscreen.retrieval.pdftext import load_manifest, read_pages

CORPUS = os.environ["CORPUS_DIR"]


def test_every_registry_clause_is_verbatim_pdf_text():
    manifest = {d["doc_id"]: d for d in load_manifest(CORPUS)["documents"]}
    clauses = json.load(open(os.path.join(CORPUS, "clauses.json"), encoding="utf-8"))["clauses"]
    pages = {}
    for c in clauses:
        if c["doc_id"] not in pages:
            pages[c["doc_id"]] = {p.page: p.text for p in read_pages(CORPUS, manifest[c["doc_id"]])}
        assert canon(c["text"]) in canon(pages[c["doc_id"]][c["page"]]), c["clause_id"]


def test_normalisation_joins_letter_spaced_headings():
    assert "Reserved Items" in normalize_text("R e s e r v e d  I t e m s")


def test_sentence_split_keeps_rupee_amounts_whole():
    text = "Should have an Average Annual Turnover of Rs.24,00,000/- during the last FIVE years. Next one."
    spans = sentences(text)
    assert text[spans[0][0]:spans[0][1]].endswith("years.")


def test_ingestion_is_idempotent_and_incremental(index):
    from tenderscreen.retrieval.ingest import run

    again = run()
    assert again["last_run"]["mode"] == "incremental"
    assert again["last_run"]["chunks_embedded"] == 0
    assert again["chunks"] == index.meta["chunks"]
    one_doc = run(reembed_doc="doe_om_2016_07")
    n_doc = sum(1 for c in index.chunks if c["doc_id"] == "doe_om_2016_07")
    assert 0 < one_doc["last_run"]["chunks_embedded"] == n_doc < one_doc["chunks"]


def test_freshness_metadata_is_recorded(index):
    meta = index.meta
    for key in ("built_at", "chunks", "embedding_backend", "corpus", "last_run"):
        assert key in meta
    assert set(meta["corpus"]["documents"]) == {"tender", "doe_om_2016_07", "doe_om_2016_09", "gfr_2017"}


def test_tender_search_finds_turnover_clause(index):
    hits = index.search("proof of turnover of at least Rs 5 Lakh during each of the last three financial years",
                        family="tender", k=4)
    assert any("ANNEX-II-7" in h["clause_ids"] for h in hits)
    assert all(h["family"] == "tender" for h in hits)


def test_policy_search_finds_startup_relaxation(index):
    hits = index.search("relaxation of prior turnover criteria for Startups in public procurement", family="policy", k=4)
    tags = {t for h in hits for t in h["clause_ids"]}
    assert tags & {"DOE-2016-09", "GFR-173", "DOE-2016-07-REPRINT"}


def test_clause_lookup_by_identifier(index):
    p = index.lookup("GFR-173")
    assert p and "relaxed for Startups" in p["text"] and p["original_page"] == 49


def test_evaluation_set_is_complete_and_valid():
    from tenderscreen.api.contracts import validate_bid
    from tenderscreen.evaluation.run import build_bid

    data = json.load(open("eval/scenarios.json", encoding="utf-8"))
    scenarios = data["scenarios"]
    assert len(scenarios) >= 20
    assert any(s["id"] == "S02" and s["expected_route"] == "escalate_to_human" for s in scenarios)
    registry = {c["clause_id"] for c in json.load(open(os.path.join(CORPUS, "clauses.json")))["clauses"]}
    for s in scenarios:
        assert s["supporting_passages"] and set(s["supporting_passages"]) <= registry, s["id"]
        bid, errors = validate_bid(build_bid(s, os.environ["EXAMPLES_DIR"]))
        assert bid is not None, (s["id"], errors)


def test_tender_states_it_was_published_on_cppp():
    clauses = json.load(open(os.path.join(CORPUS, "clauses.json"), encoding="utf-8"))["clauses"]
    pub = next(c for c in clauses if c["clause_id"] == "TENDER-1.2")
    assert "https://eprocure.gov.in/cppp/" in pub["text"]


def test_trulens_feedback_functions_score_a_real_run(pipeline, example):
    from tenderscreen.evaluation.trulens_eval import _feedbacks

    pipe, _ = pipeline()
    result, handoffs = pipe.run(example("startup_low_turnover"), {"demo_fault": "unsupported_citation"})
    output = json.dumps({"route": result["route"], "expected_route": "escalate_to_human",
                         "supporting_passages": ["ANNEX-II-7", "GFR-173"], "first_handoff": handoffs[0]})
    scores = {f.name: f.imp(output) for f in _feedbacks()}
    assert set(scores) == {"groundedness", "context_relevance", "faithfulness", "hallucination_rate", "route_accuracy"}
    assert scores["groundedness"] < 1.0 and scores["hallucination_rate"] > 0     # the injected bad citation
    assert scores["route_accuracy"] == 1.0
