"""Request correlation, HTTP golden signals and security headers (SRE-001 / SEC-005)."""

from __future__ import annotations

import time
import uuid
from typing import Any, cast

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import bind_correlation, clear_correlation, get_logger
from app.core.telemetry import HTTP_IN_FLIGHT, HTTP_LATENCY, HTTP_REQUESTS

logger = get_logger(__name__)


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Bind a correlation id (and the trace id, once tracing starts) to every log line."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        correlation_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
        request.state.correlation_id = correlation_id
        bind_correlation(correlation_id=correlation_id)
        from app.core.tracing import current_trace_id

        started = time.perf_counter()
        HTTP_IN_FLIGHT.inc()
        trace_id: str | None = None
        status_code = 500
        try:
            response = cast(Response, await call_next(request))
            status_code = response.status_code
        finally:
            elapsed = time.perf_counter() - started
            HTTP_IN_FLIGHT.dec()
            route = _route_template(request)
            HTTP_REQUESTS.labels(route=route, method=request.method, status=str(status_code)).inc()
            HTTP_LATENCY.labels(route=route, method=request.method).observe(elapsed)
            trace_id = current_trace_id()
            if trace_id:
                bind_correlation(trace_id=trace_id)
        response.headers["X-Request-Id"] = correlation_id
        if trace_id:
            response.headers["X-Trace-Id"] = trace_id
        settings = getattr(request.app.state, "settings", None)
        response.headers["X-Agent-Version"] = settings.version if settings else "unknown"
        clear_correlation()
        return response


def _route_template(request: Request) -> str:
    """Metric label with *bounded cardinality*: the route template, never the raw path.

    Using the raw path would create one time series per incident id and blow up Prometheus —
    a classic observability foot-gun this helper exists to prevent.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return str(path)
    return (
        request.url.path
        if request.url.path.startswith(("/health", "/ready", "/metrics"))
        else "unmatched"
    )
