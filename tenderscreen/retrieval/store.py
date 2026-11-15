"""Read side of the index: hybrid search and clause lookup.

Search modes
    semantic  FAISS cosine similarity on local sentence embeddings (LangChain FAISS)
    keyword   BM25 over the same chunks (exact terms such as "EMD", "Rule 170")
    hybrid    reciprocal-rank fusion of both (default)

The agent switches mode when verification fails (a "different strategy").
Every search can be restricted to one document family - ``tender`` or
``policy`` - which is how the agent deliberately looks in the *other*
corpus for provisions that modify a tender condition.
"""
from __future__ import annotations

import json
import os
import re
import threading

from langchain_community.vectorstores import FAISS
from rank_bm25 import BM25Okapi

from ..common.config import get_settings
from .embeddings import CachedEmbeddings

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = set("the of and or to a in for be is by as on with any its are this that shall should from at all which "
            "may have has been an it their such".split())

PASSAGE_FIELDS = ("chunk_id", "kind", "doc_id", "family", "source", "title", "url", "page", "original_page",
                  "clause_ids", "topics", "categories", "text")


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


class IndexNotReady(RuntimeError):
    pass


class CorpusIndex:
    def __init__(self, index_dir: str | None = None):
        s = get_settings()
        self.index_dir = index_dir or s.index_dir
        self._lock = threading.Lock()
        self._mtime = None
        self.meta: dict = {}
        self._load()

    # ------------------------------------------------------------ loading
    def _load(self) -> None:
        s = get_settings()
        meta_path = os.path.join(self.index_dir, "meta.json")
        if not os.path.exists(meta_path):
            raise IndexNotReady(f"index not built: {meta_path} missing (run the indexer)")
        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
        emb = CachedEmbeddings(s.embedding_model, s.model_cache_dir, os.path.join(self.index_dir, "vectors.sqlite"),
                               fallback=s.embedding_fallback, backend_name=meta["embedding_backend"],
                               backend=s.embedding_backend)
        if emb.name != meta["embedding_backend"]:
            raise IndexNotReady(f"index built with {meta['embedding_backend']} but runtime has {emb.name}")
        store = FAISS.load_local(os.path.join(self.index_dir, "faiss"), emb, allow_dangerous_deserialization=True)
        chunks = []
        with open(os.path.join(self.index_dir, "chunks.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                chunks.append(json.loads(line))
        self.store, self.embeddings, self.meta = store, emb, meta
        self.chunks = chunks
        self.by_id = {c["chunk_id"]: c for c in chunks}
        self.bm25 = {fam: (BM25Okapi([tokenize(c["title"] + " " + c["text"]) for c in sub]), sub)
                     for fam, sub in self._families(chunks).items()}
        self._mtime = os.path.getmtime(meta_path)

    @staticmethod
    def _families(chunks: list[dict]) -> dict[str, list[dict]]:
        fams: dict[str, list[dict]] = {"all": chunks}
        for c in chunks:
            fams.setdefault(c["family"], []).append(c)
        return fams

    def maybe_reload(self) -> None:
        """Pick up a re-run of the indexer without restarting the worker."""
        try:
            mtime = os.path.getmtime(os.path.join(self.index_dir, "meta.json"))
        except OSError:
            return
        if mtime != self._mtime:
            with self._lock:
                self._load()

    @property
    def version(self) -> str:
        return self.meta.get("index_hash", "unknown")

    # ------------------------------------------------------------ search
    @staticmethod
    def _passage(c: dict, score: float, mode: str) -> dict:
        out = {k: c.get(k) for k in PASSAGE_FIELDS}
        out["score"] = round(float(score), 4)
        out["retrieval_mode"] = mode
        return out

    def _semantic(self, query: str, family: str | None, n: int) -> list[tuple[dict, float]]:
        flt = {"family": family} if family and family != "all" else None
        docs = self.store.similarity_search_with_score(query, k=n, filter=flt, fetch_k=max(50, n * 5))
        out = []
        for doc, dist in docs:
            c = self.by_id.get(doc.metadata.get("chunk_id"))
            if c:
                out.append((c, 1.0 / (1.0 + float(dist))))
        return out

    def _keyword(self, query: str, family: str | None, n: int) -> list[tuple[dict, float]]:
        bm25, sub = self.bm25.get(family or "all", self.bm25["all"])
        scores = bm25.get_scores(tokenize(query))
        ranked = sorted(range(len(sub)), key=lambda i: scores[i], reverse=True)[:n]
        return [(sub[i], float(scores[i])) for i in ranked if scores[i] > 0]

    def search(self, query: str, family: str | None = None, mode: str = "hybrid", k: int = 4) -> list[dict]:
        self.maybe_reload()
        family = family if family in ("tender", "policy") else None
        if mode == "semantic":
            hits = self._semantic(query, family, k)
        elif mode == "keyword":
            hits = self._keyword(query, family, k)
        else:  # reciprocal rank fusion
            fused: dict[str, float] = {}
            for ranked in (self._semantic(query, family, k * 3), self._keyword(query, family, k * 3)):
                for rank, (c, _) in enumerate(ranked):
                    fused[c["chunk_id"]] = fused.get(c["chunk_id"], 0.0) + 1.0 / (60 + rank)
            hits = sorted(((self.by_id[cid], sc) for cid, sc in fused.items()), key=lambda x: x[1], reverse=True)[:k]
            hits = [(c, sc * 60) for c, sc in hits]
        return [self._passage(c, sc, mode) for c, sc in hits[:k]]

    def lookup(self, clause_id: str) -> dict | None:
        self.maybe_reload()
        c = self.by_id.get(clause_id)
        return self._passage(c, 1.0, "lookup") if c else None

    def clause_ids(self) -> list[str]:
        return sorted(c["chunk_id"] for c in self.chunks if c["kind"] == "clause")
