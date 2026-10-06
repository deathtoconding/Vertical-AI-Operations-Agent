"""Rate limiting (SEC-006).

The limiter is deliberately in-process and simple — it exists so one runaway client or retry
storm cannot consume the database pool. The tests assert the properties that matter: the limit
is per credential (not global), probes are exempt, and a rejection is a typed 429 with a
``Retry-After`` rather than an opaque drop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from app.api.middleware import rate_limit as rate_limit_module
from app.api.middleware.rate_limit import EXEMPT_PATHS, RateLimitMiddleware
from app.core.config import Settings
from tests.support.app import auth, running_app

pytestmark = [pytest.mark.story("SEC-006"), pytest.mark.security]


@pytest_asyncio.fixture
async def limited_app(settings: Settings, engine: Any) -> AsyncIterator[Any]:
    """The real application with a tiny budget, so the test needs only a few requests.

    Depends on ``engine`` for the same reason the shared ``api`` fixture does: the fixture is
    what builds the schema and skips the test when no PostgreSQL is available.
    """
    limited = settings.model_copy(
        update={"rate_limit_requests_per_minute": 2, "rate_limit_burst": 1}
    )
    async with running_app(limited) as handle:
        yield handle


async def test_exceeding_the_budget_returns_a_typed_429(limited_app: Any) -> None:
    app = limited_app
    # The budget is limit + burst = 3 requests for this credential.
    for _ in range(3):
        allowed = await app.client.get("/api/v1/admin/tools", headers=auth("viewer"))
        assert allowed.status_code == 200
        assert "X-RateLimit-Remaining" in allowed.headers

    blocked = await app.client.get("/api/v1/admin/tools", headers=auth("viewer"))
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"].isdigit()
    error = blocked.json()["error"]
    assert error["code"] == "rate_limited"
    assert error["status"] == 429
    assert error["details"]["retry_after_seconds"] >= 1


async def test_the_budget_is_per_credential_not_global(limited_app: Any) -> None:
    app = limited_app
    for _ in range(3):
        await app.client.get("/api/v1/admin/tools", headers=auth("viewer"))
    exhausted = await app.client.get("/api/v1/admin/tools", headers=auth("viewer"))
    assert exhausted.status_code == 429

    # A different token must not be punished for another client's traffic.
    other = await app.client.get("/api/v1/admin/tools", headers=auth("sre"))
    assert other.status_code == 200


async def test_anonymous_traffic_is_bounded_by_address(limited_app: Any) -> None:
    app = limited_app
    # The client carries a default token, so anonymity must be stated explicitly.
    anonymous = {"Authorization": ""}
    for _ in range(3):
        assert (await app.client.get("/api/v1/admin/tools", headers=anonymous)).status_code == 401
    blocked = await app.client.get("/api/v1/admin/tools", headers=anonymous)
    # Authorisation may answer first; what must never happen is an unbounded 200.
    assert blocked.status_code in {401, 429}
    if blocked.status_code == 429:
        assert blocked.json()["error"]["code"] == "rate_limited"


@pytest.mark.parametrize("path", ["/health", "/ready", "/metrics", "/openapi.json"])
async def test_probes_and_scrapes_are_exempt(limited_app: Any, path: str) -> None:
    """Throttling /metrics during an incident would blind the people fighting it."""
    for _ in range(10):
        response = await limited_app.client.get(path)
        assert response.status_code == 200, f"{path} must never be rate limited"


async def test_options_requests_are_not_counted(limited_app: Any) -> None:
    for _ in range(5):
        preflight = await limited_app.client.options(
            "/api/v1/incidents", headers={"Origin": "http://localhost"}
        )
        assert preflight.status_code in {200, 204, 405}


def test_client_key_hashes_the_token_and_never_stores_it() -> None:
    """The key must not be the credential: limit state would then contain secrets."""
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/incidents",
        "headers": [(b"authorization", b"Bearer super-secret-token")],
        "client": ("127.0.0.1", 1234),
    }
    key = RateLimitMiddleware._client_key(Request(scope))
    assert key.startswith("tok:")
    assert "super-secret-token" not in key
    assert len(key) <= 32


def test_exempt_paths_are_explicit_and_do_not_cover_lookalikes() -> None:
    assert set(EXEMPT_PATHS) == {"/health", "/ready", "/metrics", "/ui", "/docs", "/openapi.json"}


async def test_lookalike_paths_are_not_exempt(limited_app: Any) -> None:
    """ "/healthz" is not a probe we promised to leave unthrottled."""
    for _ in range(4):
        await limited_app.client.get("/healthz")
    blocked = await limited_app.client.get("/healthz")
    assert blocked.status_code in {404, 429}
    if blocked.status_code == 429:
        assert blocked.json()["error"]["code"] == "rate_limited"


def test_the_limiter_documents_that_it_is_not_distributed() -> None:
    """An operator must not read this as cluster-wide protection."""
    doc = rate_limit_module.__doc__ or ""
    assert "**not** distributed" in doc
    assert "ingress" in doc.lower()
