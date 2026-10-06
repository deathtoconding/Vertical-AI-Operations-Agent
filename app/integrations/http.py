"""Resilient HTTP client shared by every live integration (OPS-020).

The behaviour that matters is not "it can GET a URL" — it is what happens when the other end
misbehaves. This client implements, once, for every integration:

* **explicit timeouts** on every request (no unbounded sockets);
* **bounded retries** with exponential backoff *and jitter*, only for idempotent calls and only for
retryable conditions (connection errors, 429, 5xx) — a 401 is never retried;
* **rate-limit awareness** — ``Retry-After`` is honoured and ``X-RateLimit-Remaining`` is exported
as a metric;
* **typed errors** — a failure becomes ``IntegrationUnavailable``,
  ``IntegrationRateLimited``, ``IntegrationAuthenticationError`` or ``IntegrationBadResponse``. It
  never becomes an empty success.

Time is injectable so retry behaviour is testable without sleeping.
"""

from __future__ import annotations

import asyncio
import random
from time import perf_counter
from typing import Any

import httpx

from app.core.errors import (
    IntegrationAuthenticationError,
    IntegrationBadResponse,
    IntegrationRateLimited,
    IntegrationUnavailable,
)
from app.core.logging import get_logger
from app.core.telemetry import (
    GITHUB_RATE_LIMIT_REMAINING,
    INTEGRATION_LATENCY,
    INTEGRATION_REQUESTS,
    INTEGRATION_RETRIES,
)

logger = get_logger(__name__)

RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
DEFAULT_BACKOFF_BASE = 0.2
MAX_BACKOFF = 5.0
MAX_RESPONSE_BYTES = 2_000_000


class ResilientHttpClient:
    """A thin, opinionated wrapper around ``httpx.AsyncClient``."""

    def __init__(
        self,
        system: str,
        *,
        base_url: str = "",
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
        headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        sleep: Any = None,
        jitter: Any = None,
    ) -> None:
        self.system = system
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, max_retries)
        self.headers = {"Accept": "application/json", **(headers or {})}
        self._client = client
        self._owns_client = client is None
        self._sleep = sleep or asyncio.sleep
        self._jitter = jitter or (lambda: random.uniform(0, 0.1))  # noqa: S311 - not crypto

    # -- lifecycle ---------------------------------------------------------- #

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds, headers=self.headers)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # -- requests ----------------------------------------------------------- #

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        idempotent: bool = True,
        expect_json: bool = True,
    ) -> dict[str, Any]:
        """Perform a request, retrying only when it is safe and useful to do so."""
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        should_retry = idempotent and method.upper() in {"GET", "HEAD", "PUT", "DELETE", "POST"}
        attempts = self.max_retries + 1 if should_retry else 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            start = perf_counter()
            try:
                client = await self._get_client()
                response = await client.request(
                    method, url, json=json_body, params=params, timeout=self.timeout_seconds
                )
                elapsed = perf_counter() - start
                INTEGRATION_LATENCY.labels(system=self.system).observe(elapsed)
                self._observe_rate_limit(response)

                if response.status_code in RETRYABLE_STATUS:
                    reason = "rate_limited" if response.status_code == 429 else "server_error"
                    INTEGRATION_REQUESTS.labels(system=self.system, outcome=reason).inc()
                    if attempt < attempts:
                        await self._backoff(attempt, response, reason)
                        continue
                    if response.status_code == 429:
                        raise IntegrationRateLimited(
                            self.system,
                            f"{self.system} rate-limited the request repeatedly.",
                            details={"retry_after": response.headers.get("Retry-After")},
                        )
                    raise IntegrationUnavailable(
                        self.system,
                        f"{self.system} returned {response.status_code} after "
                        f"{attempt} attempt(s).",
                        details={"status_code": response.status_code},
                    )

                if response.status_code in {401, 403}:
                    INTEGRATION_REQUESTS.labels(system=self.system, outcome="auth_error").inc()
                    raise IntegrationAuthenticationError(
                        self.system,
                        f"{self.system} rejected the credentials (HTTP {response.status_code}).",
                        details={"status_code": response.status_code},
                    )

                if response.status_code >= 400:
                    INTEGRATION_REQUESTS.labels(system=self.system, outcome="client_error").inc()
                    raise IntegrationBadResponse(
                        self.system,
                        f"{self.system} returned HTTP {response.status_code}.",
                        details={"status_code": response.status_code},
                    )

                if len(response.content) > MAX_RESPONSE_BYTES:
                    INTEGRATION_REQUESTS.labels(system=self.system, outcome="too_large").inc()
                    raise IntegrationBadResponse(
                        self.system,
                        f"{self.system} response exceeded {MAX_RESPONSE_BYTES} bytes.",
                    )

                INTEGRATION_REQUESTS.labels(system=self.system, outcome="success").inc()
                if not expect_json:
                    return {"status_code": response.status_code, "text": response.text[:2000]}
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise IntegrationBadResponse(
                        self.system, f"{self.system} returned a non-JSON body."
                    ) from exc
                return payload if isinstance(payload, dict) else {"data": payload}

            except (httpx.TimeoutException, httpx.TransportError) as exc:
                INTEGRATION_REQUESTS.labels(system=self.system, outcome="transport_error").inc()
                last_error = exc
                if attempt < attempts:
                    await self._backoff(attempt, None, "transport_error")
                    continue
                raise IntegrationUnavailable(
                    self.system,
                    f"{self.system} is unreachable: {type(exc).__name__}.",
                    details={"attempts": attempt},
                ) from exc

        raise IntegrationUnavailable(
            self.system,
            f"{self.system} failed: {last_error}",
            details={"attempts": attempts},
        )

    async def get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return await self.request("POST", path, **kwargs)

    # -- helpers ------------------------------------------------------------ #

    async def _backoff(self, attempt: int, response: httpx.Response | None, reason: str) -> None:
        INTEGRATION_RETRIES.labels(system=self.system, reason=reason).inc()
        delay = min(MAX_BACKOFF, DEFAULT_BACKOFF_BASE * (2 ** (attempt - 1)))
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                delay = min(MAX_BACKOFF, float(retry_after))
        await self._sleep(delay + self._jitter())
        logger.info(
            "integration_retry", system=self.system, attempt=attempt, delay_seconds=round(delay, 3)
        )

    def _observe_rate_limit(self, response: httpx.Response) -> None:
        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining and remaining.isdigit():
            GITHUB_RATE_LIMIT_REMAINING.set(float(remaining))


__all__ = ["MAX_RESPONSE_BYTES", "RETRYABLE_STATUS", "ResilientHttpClient"]
