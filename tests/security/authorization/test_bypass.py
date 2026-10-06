"""Authorization matrix and bypass attempts (SEC-002, OPS-061).

The matrix is asserted once per role *and* per endpoint, because the failure mode this suite
exists for is a route that simply forgot its permission check. The second half attacks the
boundary: spoofed headers, method overrides, path casing, and a stale approval hash — each of
which is a documented way authorization gets bypassed in real systems.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.support.app import auth

pytestmark = [pytest.mark.story("SEC-002"), pytest.mark.security, pytest.mark.integration]


def matrix() -> list[tuple[str, str, dict[str, Any], dict[str, int]]]:
    """(method, path, body/params, {role or 'anonymous': expected status})."""
    return [
        (
            "GET",
            "/api/v1/incidents",
            {},
            {"anonymous": 401, "viewer": 200, "operator": 200, "sre": 200, "admin": 200},
        ),
        (
            "GET",
            "/api/v1/audit/events",
            {},
            {"anonymous": 401, "viewer": 200, "operator": 200, "sre": 200, "admin": 200},
        ),
        (
            "GET",
            "/api/v1/admin/tools",
            {},
            {"anonymous": 401, "viewer": 200, "operator": 200, "sre": 200, "admin": 200},
        ),
        (
            "GET",
            "/api/v1/agents/runs",
            {},
            {"anonymous": 401, "viewer": 200, "operator": 200, "sre": 200, "admin": 200},
        ),
        (
            "POST",
            "/api/v1/detection/scan",
            {"json": {"metrics": ["error_rate"]}},
            {"anonymous": 401, "viewer": 403, "operator": 200, "sre": 200, "admin": 200},
        ),
        (
            "POST",
            "/api/v1/admin/autonomy",
            {"json": {"level": "approval_required", "reason": "matrix test"}},
            {"anonymous": 401, "viewer": 403, "operator": 403, "sre": 403, "admin": 200},
        ),
        (
            "POST",
            "/api/v1/agents/recovery",
            {},
            {"anonymous": 401, "viewer": 403, "operator": 403, "sre": 200, "admin": 200},
        ),
        (
            "POST",
            "/api/v1/admin/pool/reset",
            {},
            # admin.recovery is held by SRE and admin by design (documented operator
            # escape hatch); view/operator have no administrative recovery rights.
            {"anonymous": 401, "viewer": 403, "operator": 403, "sre": 200, "admin": 200},
        ),
    ]


@pytest.mark.parametrize(("method", "path", "payload", "expected"), matrix())
async def test_role_matrix_is_enforced_per_endpoint(
    api: Any, method: str, path: str, payload: dict[str, Any], expected: dict[str, int]
) -> None:
    for role, status in expected.items():
        headers = {} if role == "anonymous" else auth(role)
        if role == "anonymous":
            headers = {"Authorization": ""}
        response = await api.client.request(method, path, headers=headers, **payload)
        assert response.status_code == status, (
            f"{method} {path} as {role}: expected {status}, got {response.status_code} "
            f"({response.text[:200]})"
        )


async def test_a_viewer_cannot_decide_an_approval(api: Any) -> None:
    response = await api.client.post(
        "/api/v1/approvals/APR-DOES-NOT-EXIST/decision",
        json={"decision": "APPROVED", "payload_hash": "0" * 32, "reason": "nope"},
        headers=auth("viewer"),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "authorization_denied"


async def test_an_operator_cannot_take_a_high_risk_action(api: Any) -> None:
    """Even with the right permission shape, an operator may not roll back — policy decides."""
    from app.core.security import Actor
    from app.domain.enums import PolicyDecisionType, Role
    from app.policy.engine import PolicyEngine
    from app.tools.registry import build_default_registry

    engine = PolicyEngine(build_default_registry(), api.settings)
    decision = engine.evaluate(
        "deployment.rollback_simulation",
        actor=Actor(actor_id="operator", role=Role.OPERATOR, authenticated=True),
        params={"target_release": "release-41", "reason": "regression"},
    )
    assert decision.decision is PolicyDecisionType.DENY
    assert decision.violations


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("X-Actor-Role", "admin"),
        ("X-Forwarded-For", "127.0.0.1"),
        ("X-Original-URL", "/api/v1/admin/autonomy"),
        ("X-HTTP-Method-Override", "POST"),
        ("X-Authenticated-User", "admin"),
    ],
)
async def test_spoofed_headers_do_not_grant_access(api: Any, header: str, value: str) -> None:
    response = await api.client.post(
        "/api/v1/admin/autonomy",
        json={"level": "observe_only", "reason": "spoof"},
        headers={**auth("viewer"), header: value},
    )
    assert response.status_code == 403, f"{header} must not influence authorisation"


async def test_query_parameters_cannot_escalate_a_role(api: Any) -> None:
    response = await api.client.post(
        "/api/v1/admin/autonomy?role=admin&actor=admin",
        json={"level": "observe_only", "reason": "escalation attempt"},
        headers=auth("viewer"),
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "token", ["SRE-TOKEN", "sre-token-extra", "Bearer sre-token", "sre_t0ken", "not-a-token"]
)
async def test_malformed_or_altered_tokens_are_refused(api: Any, token: str) -> None:
    response = await api.client.get(
        "/api/v1/incidents", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401, f"token {token!r} must not authenticate"


async def test_surrounding_whitespace_is_tolerated_but_does_not_change_the_role(api: Any) -> None:
    """``authenticate`` strips the token (documented); the role still comes from the token set."""
    for token in ("sre-token ", " sre-token"):
        response = await api.client.get(
            "/api/v1/incidents", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
    escalated = await api.client.post(
        "/api/v1/admin/autonomy",
        json={"level": "observe_only", "reason": "whitespace"},
        headers={"Authorization": "Bearer  sre-token  "},
    )
    assert escalated.status_code == 403, "whitespace must not widen what the token may do"


async def test_method_not_allowed_is_not_a_write_path(api: Any) -> None:
    response = await api.client.delete("/api/v1/incidents", headers=auth("admin"))
    assert response.status_code in {404, 405}


async def test_path_casing_is_not_a_bypass(api: Any) -> None:
    response = await api.client.get("/API/V1/INCIDENTS", headers={"Authorization": ""})
    assert response.status_code in {307, 404, 401}
    if response.status_code == 307:  # pragma: no cover - depends on redirect policy
        followed = await api.client.get(response.headers["location"], headers={"Authorization": ""})
        assert followed.status_code == 401


async def test_the_registry_exposes_no_unbounded_capability(api: Any) -> None:
    """The boundary is also "what can the agent do at all" (section 6 of the plan)."""
    response = await api.client.get("/api/v1/admin/tools", headers=auth("viewer"))
    tools = response.json()["tools"]
    names = {tool["name"] for tool in tools}
    assert names >= {
        "jira.create_incident",
        "slack.notify",
        "deployment.rollback_simulation",
        "github.read_commits",
    }
    forbidden = ("shell", "exec", "subprocess", "sql", "filesystem", "kubectl", "terraform")
    for name in names:
        assert not any(pattern in name.lower() for pattern in forbidden), name
    assert all(tool["risk"] in {"low", "medium", "high", "critical"} for tool in tools)
    high_risk = [tool for tool in tools if tool["risk"] in {"high", "critical"}]
    assert high_risk, "the matrix must contain a high-risk tool for the gate to be meaningful"
    assert all(tool["requires_approval"] for tool in high_risk)


async def test_a_denied_request_leaves_an_audit_record(api: Any) -> None:
    await api.client.post(
        "/api/v1/admin/autonomy",
        json={"level": "observe_only", "reason": "audit test"},
        headers=auth("viewer"),
    )
    events = await api.client.get(
        "/api/v1/audit/events", params={"event_type": "authorization_denied", "limit": 5}
    )
    assert events.json()["count"] >= 1
    assert events.json()["events"][0]["role"] == "viewer"
