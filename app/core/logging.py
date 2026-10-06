"""Structured logging with correlation ids and secret redaction (SRE-002).

Every log line carries ``request_id``, ``trace_id`` and — when the work is incident-scoped —
``incident_id`` and ``agent_run_id``. That is what makes an incident reconstructable from
logs alone, which matters when the audit table is the only other source of truth.

Redaction is a *processor*, not a convention: a secret that reaches a log call is replaced
with ``***`` before it is rendered, so "we remembered to redact" is not part of the design
(threat T-27).
"""

from __future__ import annotations

import logging
import re
import sys
from contextvars import ContextVar
from typing import Any, Final

import structlog

# --------------------------------------------------------------------------- #
# Correlation context
# --------------------------------------------------------------------------- #

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)
_incident_id: ContextVar[str | None] = ContextVar("incident_id", default=None)
_agent_run_id: ContextVar[str | None] = ContextVar("agent_run_id", default=None)
_actor: ContextVar[str | None] = ContextVar("actor", default=None)

CORRELATION_KEYS: Final[tuple[str, ...]] = (
    "request_id",
    "trace_id",
    "incident_id",
    "agent_run_id",
    "actor",
)


def bind_correlation(**values: str | None) -> None:
    """Bind correlation ids for the current async task."""
    mapping = {
        "request_id": _request_id,
        "trace_id": _trace_id,
        "incident_id": _incident_id,
        "agent_run_id": _agent_run_id,
        "actor": _actor,
    }
    for key, value in values.items():
        context = mapping.get(key)
        if context is not None and value is not None:
            context.set(value)


def current_correlation() -> dict[str, str]:
    """Return the correlation ids bound in the current context (omitting unset ones)."""
    values = {
        "request_id": _request_id.get(),
        "trace_id": _trace_id.get(),
        "incident_id": _incident_id.get(),
        "agent_run_id": _agent_run_id.get(),
        "actor": _actor.get(),
    }
    return {key: value for key, value in values.items() if value}


def clear_correlation() -> None:
    for context in (_request_id, _trace_id, _incident_id, _agent_run_id, _actor):
        context.set(None)


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #

SENSITIVE_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(token|secret|password|passwd|api[_-]?key|authorization|credential|cookie|session)",
    re.IGNORECASE,
)
SENSITIVE_VALUE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:sk-[A-Za-z0-9]{8,}|gh[pousr]_[A-Za-z0-9]{16,}|xox[baprs]-[A-Za-z0-9-]{8,}"
    r"|Bearer\s+[A-Za-z0-9._~+/-]{12,})"
)
REDACTED: Final[str] = "***"
MAX_STRING_LENGTH: Final[int] = 4000


def redact_value(key: str, value: Any) -> Any:
    """Redact by key name, by value shape, and truncate oversized strings."""
    if SENSITIVE_KEY_PATTERN.search(key):
        return REDACTED
    if isinstance(value, str):
        redacted = SENSITIVE_VALUE_PATTERN.sub(REDACTED, value)
        if len(redacted) > MAX_STRING_LENGTH:
            return f"{redacted[:MAX_STRING_LENGTH]}…[truncated {len(redacted)} chars]"
        return redacted
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, (list, tuple)):
        return [redact_value(key, item) for item in value]
    return value


def redact_mapping(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: redact_value(str(key), value) for key, value in payload.items()}


def _redaction_processor(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    return redact_mapping(event_dict)


def _correlation_processor(
    _logger: Any, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    for key, value in current_correlation().items():
        event_dict.setdefault(key, value)
    return event_dict


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

_configured = False


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    """Idempotently configure structlog and the stdlib root logger."""
    global _configured
    if _configured:
        return

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _correlation_processor,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _redaction_processor,
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str = "app") -> Any:
    """Return a bound logger carrying the current correlation ids."""
    return structlog.get_logger(name)


def reset_logging_for_tests() -> None:
    """Allow tests to reconfigure with a capture-friendly renderer."""
    global _configured
    _configured = False
    structlog.reset_defaults()
    clear_correlation()


__all__ = [
    "CORRELATION_KEYS",
    "REDACTED",
    "bind_correlation",
    "clear_correlation",
    "configure_logging",
    "current_correlation",
    "get_logger",
    "redact_mapping",
    "redact_value",
    "reset_logging_for_tests",
]
