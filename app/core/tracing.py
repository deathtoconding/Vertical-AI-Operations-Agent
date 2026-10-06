"""Distributed tracing (SRE-003).

One incident produces one trace: detection → evidence fan-out (one span per source) → LLM
call → policy → approval wait → tool execution → verification. Attributes carry the ids a
human needs to pivot between the UI, the logs and the audit trail — never evidence content
or credentials (threat T-05).

The exporter is vendor-neutral (OTel) and defaults to *none*, so tests can assert on spans
without exporting anything anywhere.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Final

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Span, Status, StatusCode

SERVICE_RESOURCE: Final[str] = "service.name"

_provider: TracerProvider | None = None
_memory_exporter: InMemorySpanExporter | None = None


def configure_tracing(
    service_name: str = "aiops-agent",
    *,
    exporter: str = "none",
    enabled: bool = True,
) -> TracerProvider:
    """Configure the global tracer provider once.

    Args:
        service_name: OTel resource service name.
        exporter: ``none`` (default), ``console``, or ``memory`` (tests).
        enabled: when false, tracing calls become no-ops.
    """
    global _provider, _memory_exporter
    if _provider is not None:
        return _provider

    provider = TracerProvider(
        resource=Resource.create({SERVICE_RESOURCE: service_name, "service.version": "1.0.0"})
    )
    if enabled:
        span_exporter: SpanExporter | None = None
        if exporter == "console":
            span_exporter = ConsoleSpanExporter()
        elif exporter == "memory":
            _memory_exporter = InMemorySpanExporter()
            span_exporter = _memory_exporter
        if span_exporter is not None:
            processor = (
                SimpleSpanProcessor(span_exporter)
                if exporter == "memory"
                else BatchSpanProcessor(span_exporter)
            )
            provider.add_span_processor(processor)

    trace.set_tracer_provider(provider)
    _provider = provider
    return provider


def get_tracer(name: str = "app") -> trace.Tracer:
    return trace.get_tracer(name)


def get_memory_exporter() -> InMemorySpanExporter | None:
    return _memory_exporter


def reset_tracing_for_tests() -> None:
    """Drop the provider so the next test configures its own memory exporter."""
    global _provider, _memory_exporter
    _provider = None
    _memory_exporter = None
    trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]  # OTel has no public reset


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Span]:
    """Start a span, attach attributes, and record failures as span status.

    ``None`` attribute values are dropped, because OTel rejects them and a missing id should
    not break the tracing path.
    """
    tracer = get_tracer()
    clean = {key: value for key, value in attributes.items() if value is not None}
    with tracer.start_as_current_span(name) as current:
        for key, value in clean.items():
            current.set_attribute(_attribute_key(key), value)
        _bind_trace_id_to_logs(current)
        try:
            yield current
        except Exception as exc:
            current.record_exception(exc)
            current.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


def _attribute_key(key: str) -> str:
    """Dotted OTel attributes: ``incident_id`` -> ``incident.id``, ``tool_name`` ->
    ``tool.name``."""
    return key.replace("_id", ".id").replace("_name", ".name").replace("_", ".")


def _bind_trace_id_to_logs(current_span: Span) -> None:
    from app.core.logging import bind_correlation

    context = current_span.get_span_context()
    if context.is_valid:
        bind_correlation(trace_id=format(context.trace_id, "032x"))


def current_trace_id() -> str | None:
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else None


def add_event(name: str, **attributes: Any) -> None:
    """Attach an event to the active span (used for timeline-worthy moments)."""
    current = trace.get_current_span()
    if current.is_recording():
        current.add_event(name, {k: v for k, v in attributes.items() if v is not None})


__all__ = [
    "add_event",
    "configure_tracing",
    "current_trace_id",
    "get_memory_exporter",
    "get_tracer",
    "reset_tracing_for_tests",
    "span",
]
