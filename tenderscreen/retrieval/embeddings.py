"""Local embeddings with a persistent per-chunk cache.

Backend: a small local sentence-embedding model run through fastembed's ONNX
runtime (no PyTorch, which keeps the container ~800 MB smaller on a laptop).
Which model is a runtime setting (EMBEDDING_MODEL); left empty, the library's
own default small English model is used, so no model identifier is stored in
this repository. Nothing is sent to a paid embedding API.

Cache: vectors are stored in SQLite keyed by sha256(backend + text). Ingestion
therefore embeds only new or changed chunks, and a single document can be
re-embedded without rebuilding the whole index (requirement L1).

EMBEDDING_BACKEND=hash selects a deterministic feature-hashing embedder
(used by the unit tests). Fallback: if the model cannot be downloaded (no
internet on first start) and EMBEDDING_FALLBACK=hash, a deterministic feature-hashing embedder is used and
recorded in the index metadata, so the system still starts. Hybrid retrieval
(BM25 + vectors) keeps quality acceptable in that degraded mode.
"""
from __future__ import annotations

import functools
import hashlib
import logging
import math
import os
import re
import sqlite3
import threading

import numpy as np
from langchain_core.embeddings import Embeddings

logger = logging.getLogger(__name__)

HASH_DIM = 384
_TOKEN = re.compile(r"[a-z0-9]+")


class HashEmbedder:
    """Signed feature hashing of unigrams + bigrams (offline fallback)."""

    name = "hash-384"

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = np.zeros(HASH_DIM, dtype=np.float32)
            toks = _TOKEN.findall(text.lower())
            feats = toks + [a + "_" + b for a, b in zip(toks, toks[1:])]
            for f in feats:
                h = int.from_bytes(hashlib.md5(f.encode()).digest()[:8], "little")
                vec[h % HASH_DIM] += 1.0 if (h >> 63) & 1 else -1.0
            norm = float(np.linalg.norm(vec)) or 1.0
            out.append((vec / norm).tolist())
        return out


class FastEmbedBackend:
    """Local ONNX sentence-embedding model. With no model configured the
    library's own default small English model is used, so no model identifier
    has to live in this repository; the one actually loaded is recorded in the
    index metadata (``embedding_backend``)."""

    def __init__(self, model_name: str | None, cache_dir: str):
        from fastembed import TextEmbedding

        os.makedirs(cache_dir, exist_ok=True)
        kwargs = {"model_name": model_name} if model_name else {}
        self.model = TextEmbedding(cache_dir=cache_dir, **kwargs)
        self.name = f"fastembed:{self.model.model_name}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [v.astype(np.float32).tolist() for v in self.model.embed(texts, batch_size=32)]


class VectorCache:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("CREATE TABLE IF NOT EXISTS vec (key TEXT PRIMARY KEY, dim INTEGER, data BLOB)")
        self.lock = threading.Lock()

    def get_many(self, keys: list[str]) -> dict[str, list[float]]:
        found: dict[str, list[float]] = {}
        with self.lock:
            for i in range(0, len(keys), 500):
                batch = keys[i:i + 500]
                q = f"SELECT key, data FROM vec WHERE key IN ({','.join('?' * len(batch))})"
                for key, data in self.conn.execute(q, batch):
                    found[key] = np.frombuffer(data, dtype=np.float32).tolist()
        return found

    def put_many(self, items: dict[str, list[float]]) -> None:
        with self.lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO vec (key, dim, data) VALUES (?, ?, ?)",
                [(k, len(v), np.asarray(v, dtype=np.float32).tobytes()) for k, v in items.items()],
            )
            self.conn.commit()


class CachedEmbeddings(Embeddings):
    """LangChain ``Embeddings`` implementation used by the FAISS store."""

    def __init__(self, model_name: str, model_cache_dir: str, vector_cache_path: str, fallback: str = "hash",
                 backend_name: str | None = None, backend: str = "local"):
        self.backend = None
        if (backend_name and backend_name.startswith("hash")) or backend == "hash":
            self.backend = HashEmbedder()
        else:
            if backend_name and backend_name.startswith("fastembed:"):
                model_name = backend_name.split(":", 1)[1]  # load exactly what the index was built with
            try:
                self.backend = FastEmbedBackend(model_name or None, model_cache_dir)
            except Exception as exc:  # download failure, unsupported model ...
                if fallback != "hash":
                    raise
                logger.warning("embedding model unavailable (%s); using hash fallback", exc)
        if self.backend is None:
            self.backend = HashEmbedder()
        self.cache = VectorCache(vector_cache_path)
        self.stats = {"embedded": 0, "cache_hits": 0}

    @property
    def name(self) -> str:
        return self.backend.name

    def _key(self, text: str) -> str:
        return hashlib.sha256((self.name + "\x00" + text).encode()).hexdigest()

    def embed_documents(self, texts: list[str], force: bool = False) -> list[list[float]]:
        keys = [self._key(t) for t in texts]
        cached = {} if force else self.cache.get_many(keys)
        missing = [(k, t) for k, t in zip(keys, texts) if k not in cached]
        if missing:
            vecs = self.backend.embed([t for _, t in missing])
            new = {k: v for (k, _), v in zip(missing, vecs)}
            self.cache.put_many(new)
            cached.update(new)
        self.stats["embedded"] += len(missing)
        self.stats["cache_hits"] += len(texts) - len(missing)
        return [cached[k] for k in keys]

    @functools.lru_cache(maxsize=2048)
    def _query_vec(self, text: str) -> tuple[float, ...]:
        return tuple(self.backend.embed([text])[0])

    def embed_query(self, text: str) -> list[float]:
        return list(self._query_vec(text))


def cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    den = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) or 1.0
    return num / den
