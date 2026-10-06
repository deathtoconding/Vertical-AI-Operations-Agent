"""Distributed tracing (SRE-003).

One incident produces one trace: detection → evidence fan-out (one span per source) → LLM
call → policy → approval wait → tool execution → verification. Attributes carry the ids a
human needs to pivot between the UI, the logs and the audit trail — never evidence content
or credentials (threat T-05).

The exporter is vendor-neutral (OTel) and defaults to *none*, so tests can assert on spans
without exporting anything anywhere.
"""

from __future__ import annotations

import hashlib
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
from opentelemetry.trace import (
    NonRecordingSpan,
    Span,
    SpanContext,
    Status,
    StatusCode,
    TraceFlags,
)

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
    # OTel exposes no public way to unset the global provider; this is the documented
    # workaround used by the SDK's own test suite. Clearing the module-level provider is not
    # enough: ``set_tracer_provider`` only installs a provider *once* per process, so a test
    # that wants its own in-memory exporter must reset that guard too — otherwise it silently
    # keeps exporting to whichever provider the first test configured.
    trace._TRACER_PROVIDER = None
    guard = getattr(trace, "_TRACER_PROVIDER_SET_ONCE", None)
    if guard is not None:  # pragma: no cover - present in every supported OTel version
        guard._done = False


def incident_context(incident_id: str) -> SpanContext:
    """A deterministic OTel span context derived from the incident id.

    An incident's lifecycle crosses process *and* request boundaries: a human approves in one
    request, the executor runs in the next. A random root span per request would split one
    incident across two traces, which is exactly the situation tracing is supposed to make
    legible. Deriving the trace id from the incident id means every span for that incident
    lands in one trace, on any replica, without storing tracing state anywhere.
    """
    digest = hashlib.sha256(f"incident:{incident_id}".encode()).digest()
    return SpanContext(
        trace_id=int.from_bytes(digest[:16], "big") or 1,
        span_id=int.from_bytes(digest[16:24], "big") or 1,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )


def _parent_context(incident_id: str | None) -> Any:
    """Reuse the current context when it already belongs to this incident's trace."""
    if not incident_id:
        return None
    derived = incident_context(incident_id)
    current = trace.get_current_span().get_span_context()
    if current.is_valid and current.trace_id == derived.trace_id:
        return None  # keep the natural parent-child hierarchy inside the trace
    return trace.set_span_in_context(NonRecordingSpan(derived))


@contextmanager
def span(name: str, incident_id: str | None = None, **attributes: Any) -> Iterator[Span]:
    """Start a span, attach attributes, and record failures as span status.

    ``incident_id`` (also accepted as ``incident_id=...`` in ``**attributes``) anchors the span
    in the incident's deterministic trace. ``None`` attribute values are dropped, because OTel
    rejects them and a missing id should not break the tracing path.
    """
    incident_id = incident_id or attributes.pop("incident_id", None)
    tracer = get_tracer()
    clean = {key: value for key, value in attributes.items() if value is not None}
    with tracer.start_as_current_span(name, context=_parent_context(incident_id)) as current:
        if incident_id:
            current.set_attribute("incident.id", incident_id)
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
    "incident_context",
    "reset_tracing_for_tests",
    "span",
]
