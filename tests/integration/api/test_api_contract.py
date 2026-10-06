"""The HTTP contract (OPS-010).

The API is the only surface an operator, the UI or a release script talks to, so its contract
is tested directly: every documented route exists, errors have one shape, authentication is not
optional, and the OpenAPI document the repository commits still matches the application.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.support.app import auth

pytestmark = [pytest.mark.story("OPS-010"), pytest.mark.integration]

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT = REPO_ROOT / "docs" / "architecture" / "openapi.json"

#: Route prefixes that must exist for the documented workflows to be possible.
REQUIRED_PATHS = (
    "/health",
    "/ready",
    "/metrics",
    "/api/v1/incidents",
    "/api/v1/incidents/{incident_id}",
    "/api/v1/incidents/{incident_id}/evidence",
    "/api/v1/approvals",
    "/api/v1/approvals/{approval_id}/decision",
    "/api/v1/agents/runs/{run_id}",
    "/api/v1/agents/runs/{run_id}/resume",
    "/api/v1/detection/simulate",
    "/api/v1/release/current",
    "/api/v1/audit/verify",
)


async def test_every_required_route_is_mounted(api: Any) -> None:
    document = api.app.openapi()
    missing = [path for path in REQUIRED_PATHS if path not in document["paths"]]
    assert not missing, f"routes missing from the application: {missing}"


async def test_the_committed_openapi_contract_matches_the_application(api: Any) -> None:
    """A stale contract is how clients discover a breaking change in production."""
    committed = json.loads(CONTRACT.read_text(encoding="utf-8"))
    live = api.app.openapi()
    assert set(live["paths"]) == set(committed["paths"])
    for path, operations in committed["paths"].items():
        assert set(live["paths"][path]) == set(operations), f"methods changed for {path}"


async def test_api_is_versioned_and_probes_are_not(api: Any) -> None:
    assert api.settings.api_prefix == "/api/v1"
    for probe in ("/health", "/ready", "/metrics"):
        assert (await api.client.get(probe)).status_code == 200


async def test_health_is_dependency_free_while_readiness_reports_dependencies(api: Any) -> None:
    health = await api.client.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok"
    assert body["version"] == api.settings.version
    assert body["simulated"] is True, "the sandbox must announce itself"

    ready = await api.client.get("/ready")
    assert ready.status_code in {200, 503}
    payload = ready.json()
    assert payload["status"] in {"ok", "ready", "degraded"}
    assert payload["database"]["status"] in {"ok", "unavailable"}
    assert isinstance(payload.get("integrations", {}), dict)


async def test_metrics_expose_the_prometheus_contract(api: Any) -> None:
    response = await api.client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "aiops_http_requests_total" in response.text


async def test_unknown_routes_return_the_structured_error_envelope(api: Any) -> None:
    """Routed-away requests get the same envelope as domain errors (``http_404``)."""
    response = await api.client.get("/api/v1/definitely-not-a-route")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "http_404"
    assert error["status"] == 404
    assert error["message"]


async def test_unknown_incidents_return_the_structured_error_envelope(api: Any) -> None:
    response = await api.client.get("/api/v1/incidents/INC-DOES-NOT-EXIST")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["details"]["incident_id"] == "INC-DOES-NOT-EXIST"


async def test_missing_credentials_are_rejected_with_401(api: Any) -> None:
    """There is no anonymous read path: a reachable port is not an open door."""
    response = await api.client.get("/api/v1/incidents", headers={"Authorization": ""})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


async def test_an_unknown_token_is_rejected_and_audited(api: Any) -> None:
    response = await api.client.get(
        "/api/v1/incidents", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"

    audit = await api.client.get(
        "/api/v1/audit/events", params={"event_type": "authentication_failed"}
    )
    assert audit.status_code == 200
    assert audit.json()["events"], "a failed authentication must leave a trace"


async def test_invalid_request_bodies_are_rejected_with_a_validation_error(api: Any) -> None:
    response = await api.client.post("/api/v1/detection/scan", json={"metrics": ["nope"]})
    assert response.status_code in {400, 422}
    assert response.json()["error"]["code"] in {"validation_failed", "request_validation_failed"}


async def test_responses_carry_the_security_headers_and_a_request_id(api: Any) -> None:
    response = await api.client.get("/api/v1/incidents")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers.get("X-Request-Id"), "requests must be correlatable"


async def test_authorization_denials_are_403_and_audited(api: Any) -> None:
    """A 403 that leaves no trace is indistinguishable from a bug."""
    response = await api.client.post(
        "/api/v1/admin/autonomy",
        json={"level": "observe_only", "reason": "test"},
        headers=auth("viewer"),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "authorization_denied"

    audit = await api.client.get(
        "/api/v1/audit/events", params={"event_type": "authorization_denied"}
    )
    assert audit.json()["events"], "the denial must be recorded"


async def test_the_ui_is_served_from_the_same_origin(api: Any) -> None:
    index = await api.client.get("/")
    assert index.status_code == 200
    assert "text/html" in index.headers["content-type"]
    assert "Operations Agent Console" in index.text
    assert "/ui/app.js" in index.text, "the console must load its assets from this origin"
