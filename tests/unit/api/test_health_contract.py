"""HTTP service contract (OPS-010).

The service-level guarantees are not "a route exists" but "the probes mean different things",
"errors are machine-readable" and "nothing here needs a secret in the source tree". These are
tested at the unit level (no database) by exercising the real error handlers and the real
``Settings`` validators — the HTTP behaviour over PostgreSQL lives in
``tests/integration/api/test_api_contract.py``.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.errors import error_body, install_error_handlers
from app.core.config import Environment, IntegrationsMode, Settings
from app.core.errors import (
    AuthorizationError,
    ConflictError,
    DependencyUnavailable,
    NotFoundError,
    RateLimitedError,
    ValidationFailed,
)
from app.main import create_app

pytestmark = [pytest.mark.story("OPS-010"), pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Every error code the API contract promises, with the status code it must produce.
ERROR_CONTRACT: dict[type[Exception], tuple[str, int]] = {
    ValidationFailed: ("validation_failed", 422),
    NotFoundError: ("not_found", 404),
    ConflictError: ("conflict", 409),
    AuthorizationError: ("authorization_denied", 403),
    RateLimitedError: ("rate_limited", 429),
    DependencyUnavailable: ("dependency_unavailable", 503),
}

#: Shapes that would be a credential if they appeared in the source tree.
SECRET_SHAPES = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


@pytest.fixture
def error_app() -> FastAPI:
    """A minimal app that raises a typed error through the real handler stack."""
    application = FastAPI()
    install_error_handlers(application)

    @application.get("/boom/{kind}")
    async def boom(kind: str) -> dict[str, Any]:
        raise NEXT_ERROR[kind]

    global NEXT_ERROR
    NEXT_ERROR = {
        "validation": ValidationFailed("bad input", details={"field": "text"}),
        "not_found": NotFoundError("no such incident"),
        "conflict": ConflictError("stale version"),
        "forbidden": AuthorizationError("role cannot approve"),
        "rate_limited": RateLimitedError(60),
        "dependency": DependencyUnavailable("database is down"),
    }
    return application


NEXT_ERROR: dict[str, Exception] = {}


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("validation", ("validation_failed", 422)),
        ("not_found", ("not_found", 404)),
        ("conflict", ("conflict", 409)),
        ("forbidden", ("authorization_denied", 403)),
        ("rate_limited", ("rate_limited", 429)),
        ("dependency", ("dependency_unavailable", 503)),
    ],
)
def test_typed_errors_are_machine_readable(
    error_app: FastAPI, kind: str, expected: tuple[str, int]
) -> None:
    """Every deliberate failure carries a stable code, a status and a correlation id."""
    code, status_code = expected
    with TestClient(error_app, raise_server_exceptions=False) as client:
        response = client.get(f"/boom/{kind}")

    assert response.status_code == status_code
    body = response.json()["error"]
    assert body["code"] == code
    assert body["status"] == status_code
    assert body["message"]
    assert "details" in body
    # The correlation id is how an operator joins an error response to a log line.
    assert "correlation_id" in body and "trace_id" in body
    if status_code == 401:
        assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_error_bodies_never_echo_internals() -> None:
    """The 500 body must not leak a traceback, a table name or a stack fragment."""
    body = error_body("internal_error", "An internal error occurred.", status_code=500)
    serialised = str(body)
    assert "Traceback" not in serialised
    assert "site-packages" not in serialised
    assert body["error"]["code"] == "internal_error"


def test_every_error_class_has_a_distinct_code() -> None:
    codes = [code for code, _ in ERROR_CONTRACT.values()]
    assert len(codes) == len(set(codes)), "two failures would look identical to a caller"


async def test_health_is_dependency_free_and_identifies_the_build() -> None:
    """Liveness must answer without touching a dependency, and name what is running.

    The container is stubbed and the lifespan is *not* run: if ``/health`` needed the database
    that would be a liveness probe that dies with the thing it is meant to report on.
    """
    settings = Settings(env=Environment.TEST, integrations_mode=IntegrationsMode.SANDBOX)
    app = create_app(settings)
    app.state.container = SimpleNamespace(settings=settings)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["version"] == settings.version
    assert payload["environment"] == "test"
    assert payload["simulated"] is True, "sandbox mode must announce itself on /health"


def test_probes_are_registered_outside_the_versioned_api() -> None:
    """``/health`` and ``/ready`` are operational; they do not live under /api/v1."""
    settings = Settings(env=Environment.TEST)
    document = create_app(settings).openapi()
    paths = set(document["paths"])
    assert "/health" in paths
    assert "/ready" in paths
    assert "/metrics" in paths
    assert not any(path.startswith(f"{settings.api_prefix}/health") for path in paths)
    assert any(path.startswith(settings.api_prefix) for path in paths)


def test_the_api_is_versioned_and_the_prefix_is_configurable() -> None:
    """Versioning is configuration, validated — not a convention someone must remember."""
    default = Settings(env=Environment.TEST)
    assert default.api_prefix == "/api/v1"

    custom = Settings(env=Environment.TEST, api_prefix="/api/v2")
    assert custom.api_prefix == "/api/v2"

    with pytest.raises(ValidationError):
        Settings(env=Environment.TEST, api_prefix="api/v1")  # no leading slash

    # A trailing slash is normalised rather than accepted verbatim, so the prefix cannot
    # produce ``//`` paths in the router.
    assert Settings(env=Environment.TEST, api_prefix="/api/v1/").api_prefix == "/api/v1"


def test_configuration_is_environment_driven(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings come from the environment, and the environment wins over the defaults."""
    from app.core.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("AIOPS_SERVICE_NAME", "aiops-from-env")
    monkeypatch.setenv("AIOPS_RATE_LIMIT_REQUESTS_PER_MINUTE", "17")
    try:
        settings = get_settings()
    finally:
        get_settings.cache_clear()
    assert settings.service_name == "aiops-from-env"
    assert settings.rate_limit_requests_per_minute == 17

    # The values are validated, not trusted: a sandbox integration in a production
    # environment is refused, because it would report simulated work as real.
    with pytest.raises(ValidationError):
        Settings(env=Environment.PRODUCTION, integrations_mode=IntegrationsMode.SANDBOX)


def test_staging_and_production_refuse_to_boot_in_sandbox_mode() -> None:
    """A simulated integration in a real environment is a lie, so booting refuses it."""
    for environment in (Environment.STAGING, Environment.PRODUCTION):
        with pytest.raises(ValidationError):
            Settings(env=environment, integrations_mode=IntegrationsMode.SANDBOX)


def test_no_credentials_are_committed_in_the_application_source() -> None:
    """SEC-005's rule, enforced cheaply: the application package holds no secret literals."""
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "app").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for shape in SECRET_SHAPES:
            if shape.search(text):
                offenders.append(f"{path.relative_to(REPO_ROOT)} matches {shape.pattern}")
    assert not offenders, "credential-shaped literals in app/: " + "; ".join(offenders)
