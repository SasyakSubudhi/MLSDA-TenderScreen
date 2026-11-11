"""Runtime configuration.

Every environment-specific value (URLs, model identifiers, credentials, limits)
is read from environment variables injected at runtime by Docker Compose
(see .env.example). Nothing environment-specific is hard-coded here: the
defaults below are only safe, non-secret fallbacks so unit tests can run.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    service_name: str = field(default_factory=lambda: _env("SERVICE_NAME", "tenderscreen"))
    redis_url: str = field(default_factory=lambda: _env("REDIS_URL", "redis://localhost:6379/0"))

    # paths
    corpus_dir: str = field(default_factory=lambda: _env("CORPUS_DIR", "corpus"))
    index_dir: str = field(default_factory=lambda: _env("INDEX_DIR", "data/index"))
    model_cache_dir: str = field(default_factory=lambda: _env("MODEL_CACHE_DIR", "data/models"))
    prompts_dir: str = field(default_factory=lambda: _env("PROMPTS_DIR", "prompts"))
    examples_dir: str = field(default_factory=lambda: _env("EXAMPLES_DIR", "examples"))

    # embeddings / retrieval
    embedding_model: str = field(default_factory=lambda: _env("EMBEDDING_MODEL", ""))
    embedding_fallback: str = field(default_factory=lambda: _env("EMBEDDING_FALLBACK", "hash"))
    chunk_size: int = field(default_factory=lambda: _int("CHUNK_SIZE", 900))
    chunk_overlap: int = field(default_factory=lambda: _int("CHUNK_OVERLAP", 150))
    retrieval_k: int = field(default_factory=lambda: _int("RETRIEVAL_K", 4))
    retrieval_mode: str = field(default_factory=lambda: _env("RETRIEVAL_MODE", "hybrid"))
    retrieval_cache_enabled: bool = field(default_factory=lambda: _bool("RETRIEVAL_CACHE_ENABLED", True))
    retrieval_cache_ttl_s: int = field(default_factory=lambda: _int("RETRIEVAL_CACHE_TTL_S", 3600))

    # LLM
    llm_gateway_url: str = field(default_factory=lambda: _env("LLM_GATEWAY_URL", "http://localhost:8100"))
    llm_call_timeout_s: float = field(default_factory=lambda: _float("LLM_CALL_TIMEOUT_S", 30.0))
    prompt_version: str = field(default_factory=lambda: _env("PROMPT_VERSION", "v2"))

    # agent loop bounds (A1)
    max_steps: int = field(default_factory=lambda: _int("AGENT_MAX_STEPS", 12))
    max_tool_calls: int = field(default_factory=lambda: _int("AGENT_MAX_TOOL_CALLS", 16))
    run_timeout_s: float = field(default_factory=lambda: _float("RUN_TIMEOUT_S", 120.0))
    tool_timeout_s: float = field(default_factory=lambda: _float("TOOL_TIMEOUT_S", 10.0))
    max_verification_attempts: int = field(default_factory=lambda: _int("MAX_VERIFICATION_ATTEMPTS", 2))

    # queue
    queue_lease_ms: int = field(default_factory=lambda: _int("QUEUE_LEASE_MS", 60000))
    queue_max_deliveries: int = field(default_factory=lambda: _int("QUEUE_MAX_DELIVERIES", 3))

    # API
    rate_limit_per_min: int = field(default_factory=lambda: _int("RATE_LIMIT_PER_MIN", 30))
    run_cache_enabled: bool = field(default_factory=lambda: _bool("RUN_CACHE_ENABLED", True))
    demo_mode: bool = field(default_factory=lambda: _bool("DEMO_MODE", True))
    live_eval_sample_rate: float = field(default_factory=lambda: _float("LIVE_EVAL_SAMPLE_RATE", 0.25))

    # telemetry
    otlp_endpoint: str = field(default_factory=lambda: _env("OTEL_EXPORTER_OTLP_ENDPOINT", ""))
    metrics_port: int = field(default_factory=lambda: _int("METRICS_PORT", 9100))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))


def get_settings() -> Settings:
    """Build settings from the current environment (cheap; call freely)."""
    return Settings()


settings = get_settings()
