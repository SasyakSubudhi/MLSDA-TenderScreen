"""Shared test setup: an offline index (hash embeddings) in a temp folder,
fakeredis instead of Redis, and the in-process stub model. No Docker or
network is needed to run `pytest`."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_TMP = tempfile.mkdtemp(prefix="tenderscreen-test-")
os.environ.update({
    "CORPUS_DIR": os.path.join(ROOT, "corpus"),
    "PROMPTS_DIR": os.path.join(ROOT, "prompts"),
    "EXAMPLES_DIR": os.path.join(ROOT, "examples"),
    "INDEX_DIR": os.path.join(_TMP, "index"),
    "MODEL_CACHE_DIR": os.path.join(_TMP, "models"),
    "EMBEDDING_MODEL": "",          # offline hash embeddings keep tests fast and hermetic
    "EMBEDDING_FALLBACK": "hash",
    "LLM_PRICE_INPUT_PER_1M": "0.10",
    "LLM_PRICE_OUTPUT_PER_1M": "0.40",
    "RATE_LIMIT_PER_MIN": "5",
    "DEMO_MODE": "true",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "",
})

import json  # noqa: E402

import fakeredis  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(scope="session")
def index():
    from tenderscreen.retrieval.ingest import run
    from tenderscreen.retrieval.store import CorpusIndex

    run()
    return CorpusIndex()


@pytest.fixture()
def fake_redis(monkeypatch):
    from tenderscreen.common import redis_client

    r = fakeredis.FakeRedis(decode_responses=True)
    redis_client.get_redis.cache_clear()
    monkeypatch.setattr(redis_client, "get_redis", lambda: r)
    for mod in ("tenderscreen.common.queue", "tenderscreen.common.runstore", "tenderscreen.api.main",
                "tenderscreen.api.ratelimit", "tenderscreen.llm.gateway"):
        if mod in sys.modules:
            monkeypatch.setattr(sys.modules[mod], "get_redis", lambda: r, raising=False)
    return r


@pytest.fixture()
def example():
    def load(name: str) -> dict:
        with open(os.path.join(ROOT, "examples", name + ".json"), encoding="utf-8") as fh:
            return json.load(fh)
    return load


@pytest.fixture()
def pipeline(index):
    from tenderscreen.agents.pipeline import Pipeline
    from tenderscreen.common.runstore import MemoryTrace
    from tenderscreen.llm.client import InProcessStubClient

    def make(fail: bool = False):
        trace = MemoryTrace()
        return Pipeline(index, InProcessStubClient(fail=fail), trace), trace
    return make
