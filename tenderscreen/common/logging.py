"""Structured JSON logging with a correlation ID.

The correlation ID is the run identifier. It is stored in a context variable so
that every log line written while a run is being processed carries it, in the
API, both agent workers and the LLM gateway.
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import time

correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")
agent_name: contextvars.ContextVar[str] = contextvars.ContextVar("agent_name", default="-")


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "correlation_id": correlation_id.get(),
            "agent": agent_name.get(),
            "msg": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(service: str, level: str = "INFO") -> logging.Logger:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root.addHandler(handler)
    root.setLevel(level.upper())
    # third-party libraries are noisy at INFO
    for noisy in ("httpx", "httpcore", "urllib3", "faiss", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(service)


def log(logger: logging.Logger, level: int, msg: str, **fields) -> None:
    """Log with structured key/value fields."""
    logger.log(level, msg, extra={"fields": fields})
