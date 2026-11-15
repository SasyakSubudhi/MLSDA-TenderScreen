"""Corpus ingestion pipeline: read -> parse -> chunk -> embed -> index.

Idempotent and incremental. Running it twice changes nothing the second time;
changing one PDF re-embeds only that PDF's chunks and patches the FAISS index
in place (delete stale ids, add new ones) rather than rebuilding it.

Chunking strategy (see docs/RETRIEVAL.md):
  * clause units - each curated clause from corpus/clauses.json is indexed as
    one intact passage, with metadata naming its topics (prior turnover,
    EMD ...) and the bidder categories it concerns (startup, MSE ...). This is
    what lets a category-aware query find a policy relaxation the tender
    never mentions.
  * page windows - every page is also split into overlapping windows
    (CHUNK_SIZE / CHUNK_OVERLAP characters) so text outside the curated
    clauses is still searchable. Windows never cross a page boundary, so
    every citation carries an exact page number.

Usage:
    python -m tenderscreen.retrieval.ingest              # incremental build
    python -m tenderscreen.retrieval.ingest --force      # re-embed everything
    python -m tenderscreen.retrieval.ingest --reembed-doc gfr_2017
    python -m tenderscreen.retrieval.ingest --check      # print freshness only
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time

from langchain_community.vectorstores import FAISS
from langchain_text_splitters import RecursiveCharacterTextSplitter

from ..common.config import get_settings
from ..common.logging import log, setup_logging
from .embeddings import CachedEmbeddings
from .normalize import canon
from .pdftext import load_manifest, read_pages

logger = logging.getLogger("indexer")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def load_clauses(corpus_dir: str) -> list[dict]:
    with open(os.path.join(corpus_dir, "clauses.json"), encoding="utf-8") as fh:
        return json.load(fh)["clauses"]


def tag_clauses(text: str, page_clauses: list[dict]) -> list[str]:
    """Clause ids whose opening (or closing) words appear in this window."""
    ctext = canon(text)
    tags = []
    for c in page_clauses:
        ct = canon(c["text"])
        if ct[:60] in ctext or ct[-60:] in ctext:
            tags.append(c["clause_id"])
    return tags


def build_chunks(corpus_dir: str, chunk_size: int, chunk_overlap: int) -> tuple[list[dict], dict]:
    manifest = load_manifest(corpus_dir)
    clauses = load_clauses(corpus_dir)
    docs = {d["doc_id"]: d for d in manifest["documents"]}
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size, chunk_overlap=chunk_overlap, separators=[". ", "; ", ", ", " "], keep_separator="end"
    )
    chunks: list[dict] = []
    doc_info: dict[str, dict] = {}
    pages_total = 0
    for doc in manifest["documents"]:
        path = os.path.join(corpus_dir, doc["file"])
        digest = sha256_file(path)
        if doc.get("sha256") and digest != doc["sha256"]:
            log(logger, logging.WARNING, "document hash differs from manifest", doc_id=doc["doc_id"])
        pages = read_pages(corpus_dir, doc)
        pages_total += len(pages)
        doc_info[doc["doc_id"]] = {"sha256": digest, "version": doc.get("version"), "pages": len(pages)}
        for page in pages:
            page_clauses = [c for c in clauses if c["doc_id"] == doc["doc_id"] and c["page"] == page.page]
            for i, window in enumerate(splitter.split_text(page.text)):
                window = window.strip()
                if len(window) < 40:
                    continue
                chunks.append({
                    "chunk_id": f"{doc['doc_id']}:p{page.page}:w{i}",
                    "kind": "window",
                    "doc_id": doc["doc_id"],
                    "family": doc["family"],
                    "source": doc["short"],
                    "title": doc["title"],
                    "url": doc["url"],
                    "page": page.page,
                    "original_page": page.original_page,
                    "clause_ids": tag_clauses(window, page_clauses),
                    "topics": [],
                    "categories": [],
                    "doc_sha256": digest,
                    "text": window,
                })
    for c in clauses:
        doc = docs[c["doc_id"]]
        chunks.append({
            "chunk_id": c["clause_id"],
            "kind": "clause",
            "doc_id": c["doc_id"],
            "family": c["family"],
            "source": c["source"],
            "title": c["title"],
            "url": c["url"],
            "page": c["page"],
            "original_page": c["original_page"],
            "clause_ids": [c["clause_id"]],
            "topics": c["topics"],
            "categories": c["categories"],
            "doc_sha256": doc_info[c["doc_id"]]["sha256"],
            # the embedded text includes the clause title so topic words help retrieval
            "text": c["text"],
        })
    for ch in chunks:
        # the diff key covers metadata too, so a relabelled clause is re-indexed; its vector still
        # comes from the vector cache, which is keyed by text alone
        meta = {k: ch[k] for k in ("source", "title", "page", "original_page", "clause_ids", "topics", "categories")}
        ch["text_sha256"] = hashlib.sha256((ch["text"] + json.dumps(meta, sort_keys=True)).encode()).hexdigest()
    return chunks, {"documents": doc_info, "pages": pages_total, "corpus_version": manifest.get("corpus_version")}


def embed_text(ch: dict) -> str:
    """Text actually embedded: title/topic metadata helps category-aware queries."""
    if ch["kind"] == "clause":
        extra = " ".join(ch["topics"] + ch["categories"])
        return f"{ch['title']}. {extra}. {ch['text']}"
    return ch["text"]


def run(force: bool = False, reembed_doc: str | None = None) -> dict:
    s = get_settings()
    t0 = time.time()
    os.makedirs(s.index_dir, exist_ok=True)
    chunks, corpus_meta = build_chunks(s.corpus_dir, s.chunk_size, s.chunk_overlap)
    t_parse = time.time() - t0

    meta_path = os.path.join(s.index_dir, "meta.json")
    old_meta = {}
    if os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as fh:
            old_meta = json.load(fh)

    emb = CachedEmbeddings(s.embedding_model, s.model_cache_dir, os.path.join(s.index_dir, "vectors.sqlite"),
                           fallback=s.embedding_fallback, backend=s.embedding_backend)
    backend_changed = old_meta.get("embedding_backend") not in (None, emb.name)

    faiss_dir = os.path.join(s.index_dir, "faiss")
    old_chunks: dict[str, str] = {}
    old_path = os.path.join(s.index_dir, "chunks.jsonl")
    if os.path.exists(old_path):
        with open(old_path, encoding="utf-8") as fh:
            for line in fh:
                c = json.loads(line)
                old_chunks[c["chunk_id"]] = c["text_sha256"]

    new_ids = {c["chunk_id"]: c for c in chunks}
    rebuild = force or backend_changed or not os.path.exists(os.path.join(faiss_dir, "index.faiss"))
    if rebuild:
        to_add = list(new_ids.values())
        to_delete: list[str] = []
    else:
        to_add = [c for cid, c in new_ids.items() if old_chunks.get(cid) != c["text_sha256"]
                  or (reembed_doc and c["doc_id"] == reembed_doc)]
        to_delete = [cid for cid in old_chunks if cid not in new_ids or cid in {c["chunk_id"] for c in to_add}]

    t1 = time.time()
    force_embed = force or bool(reembed_doc)
    vectors = emb.embed_documents([embed_text(c) for c in to_add], force=force_embed) if to_add else []
    t_embed = time.time() - t1

    meta_fields = ["chunk_id", "kind", "doc_id", "family", "source", "title", "url", "page", "original_page",
                   "clause_ids", "topics", "categories"]
    pairs = [(c["text"], v) for c, v in zip(to_add, vectors)]
    metadatas = [{k: c[k] for k in meta_fields} for c in to_add]
    ids = [c["chunk_id"] for c in to_add]

    if rebuild:
        store = FAISS.from_embeddings(pairs, emb, metadatas=metadatas, ids=ids)
    else:
        store = FAISS.load_local(faiss_dir, emb, allow_dangerous_deserialization=True)
        if to_delete:
            store.delete(ids=[i for i in to_delete if i in store.index_to_docstore_id.values()])
        if pairs:
            store.add_embeddings(pairs, metadatas=metadatas, ids=ids)
    changed = bool(rebuild or to_add or to_delete)
    if changed:
        store.save_local(faiss_dir)
        with open(old_path + ".tmp", "w", encoding="utf-8") as fh:
            for c in chunks:
                fh.write(json.dumps(c, ensure_ascii=False) + "\n")
        os.replace(old_path + ".tmp", old_path)

    elapsed = time.time() - t0
    index_hash = hashlib.sha256("".join(sorted(c["text_sha256"] for c in chunks)).encode()).hexdigest()[:16]
    meta = {
        "built_at": time.time() if changed else old_meta.get("built_at", time.time()),
        "checked_at": time.time(),
        "index_hash": index_hash + ":" + emb.name,
        "embedding_backend": emb.name,
        "chunk_size": s.chunk_size,
        "chunk_overlap": s.chunk_overlap,
        "chunks": len(chunks),
        "clause_units": sum(1 for c in chunks if c["kind"] == "clause"),
        "corpus": corpus_meta,
        "last_run": {
            "mode": "rebuild" if rebuild else "incremental",
            "chunks_reindexed": len(to_add),
            "chunks_embedded": emb.stats["embedded"],
            "chunks_deleted": len(to_delete),
            "embedding_cache_hits": emb.stats["cache_hits"],
            "seconds_total": round(elapsed, 3),
            "seconds_parse": round(t_parse, 3),
            "seconds_embed": round(t_embed, 3),
            "pages_per_s": round(corpus_meta["pages"] / max(t_parse, 1e-6), 2),
            "chunks_per_s": round(len(chunks) / max(elapsed, 1e-6), 2),
        },
    }
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
    return meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build or update the corpus index")
    parser.add_argument("--force", action="store_true", help="re-embed every chunk and rebuild the index")
    parser.add_argument("--reembed-doc", help="re-embed only the chunks of this doc_id")
    parser.add_argument("--check", action="store_true", help="print index freshness and exit")
    args = parser.parse_args(argv)
    s = get_settings()
    setup_logging("indexer", s.log_level)
    if args.check:
        path = os.path.join(s.index_dir, "meta.json")
        if not os.path.exists(path):
            print("no index built yet")
            return 1
        print(open(path, encoding="utf-8").read())
        return 0
    meta = run(force=args.force, reembed_doc=args.reembed_doc)
    log(logger, logging.INFO, "index ready", chunks=meta["chunks"], backend=meta["embedding_backend"],
        **meta["last_run"])
    print(json.dumps(meta["last_run"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
