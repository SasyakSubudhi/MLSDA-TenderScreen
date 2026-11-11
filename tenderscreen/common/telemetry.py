"""OpenTelemetry tracing helpers.

Traces are exported over OTLP/HTTP to Jaeger. The trace context is carried
across the Redis queue by storing a W3C ``traceparent`` in the job payload,
so the API submission, the research agent, the verifier agent and the LLM
gateway calls all appear as one trace in Jaeger.
"""
from __future__ import annotations

import contextlib
from typing import Iterator

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_configured = False


def setup_tracing(service_name: str, otlp_endpoint: str) -> None:
    global _configured
    if _configured:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(endpoint=otlp_endpoint.rstrip("/") + "/v1/traces", timeout=5)
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    try:  # propagate trace headers on outgoing httpx calls
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception:  # pragma: no cover - instrumentation is optional
        pass
    _configured = True


def tracer(name: str = "tenderscreen"):
    return trace.get_tracer(name)


def inject_context() -> dict:
    """Return a carrier dict containing the current trace context."""
    carrier: dict = {}
    propagate.inject(carrier)
    return carrier


@contextlib.contextmanager
def attached_context(carrier: dict | None) -> Iterator[None]:
    """Run a block inside the trace context extracted from ``carrier``."""
    token = otel_context.attach(propagate.extract(carrier or {}))
    try:
        yield
    finally:
        otel_context.detach(token)


def current_trace_id() -> str:
    span = trace.get_current_span()
    ctx = span.get_span_context()
    return format(ctx.trace_id, "032x") if ctx and ctx.trace_id else ""
