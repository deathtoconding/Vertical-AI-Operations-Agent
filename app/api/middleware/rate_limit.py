"""In-process rate limiting (SEC-006).

Deliberately simple, and honest about what it is: a fixed window per token/address that stops a
runaway client or retry storm from consuming the database pool.

It is **not** distributed protection. With more than one replica each instance counts its own
traffic, so the effective limit is ``replicas x limit``; a real deployment puts a limiter at the
ingress as well. That limit is documented in ``docs/sre/slos.md`` rather than implied away.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any, cast

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.logging import get_logger
from app.core.telemetry import RATE_LIMIT_REJECTIONS

logger = get_logger(__name__)

#: Paths never rate-limited: probes and scraping must not be throttled by agent traffic.
EXEMPT_PATHS = ("/health", "/ready", "/metrics", "/ui", "/docs", "/openapi.json")


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window-per-second token bucket, per client, in process memory."""

    def __init__(self, app: Any, *, limit_per_minute: int = 600, burst: int = 60) -> None:
        super().__init__(app)
        self.limit_per_minute = max(1, limit_per_minute)
        self.burst = max(1, burst)
        self.window_seconds = 60.0
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        path = request.url.path
        # Exact match or a subtree of an exempt path: "/health" and "/ui/app.js" are exempt,
        # "/healthcheck" is not a probe we promised to leave unthrottled.
        exempt = any(path == prefix or path.startswith(f"{prefix}/") for prefix in EXEMPT_PATHS)
        if request.method == "OPTIONS" or exempt:
            return cast(Response, await call_next(request))

        client = self._client_key(request)
        now = time.monotonic()
        hits = self._hits[client]
        while hits and now - hits[0] > self.window_seconds:
            hits.popleft()

        limit = self.limit_per_minute + self.burst
        if len(hits) >= limit:
            retry_after = max(1, int(self.window_seconds - (now - hits[0])))
            RATE_LIMIT_REJECTIONS.labels(scope="http").inc()
            logger.warning("rate_limited", path=path, client=client)
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "rate_limited",
                        "message": "Too many requests. Retry later.",
                        "details": {"retry_after_seconds": retry_after},
                        "status": 429,
                    }
                },
                headers={"Retry-After": str(retry_after)},
            )

        hits.append(now)
        response = cast(Response, await call_next(request))
        remaining = max(0, limit - len(hits))
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response

    @staticmethod
    def _client_key(request: Request) -> str:
        """Identify the caller by token when present, else by peer address.

        Preferring the token means one noisy client cannot exhaust another's budget; falling
        back to the address keeps unauthenticated floods bounded too.
        """
        authorization = request.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            import hashlib

            return "tok:" + hashlib.sha256(authorization[7:].encode()).hexdigest()[:16]
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return "ip:" + forwarded.split(",")[0].strip()
        client = request.client
        return "ip:" + (client.host if client else "unknown")


__all__ = ["EXEMPT_PATHS", "RateLimitMiddleware"]
