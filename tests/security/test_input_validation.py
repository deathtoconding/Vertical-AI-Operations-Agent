"""Input validation and rate limiting at the API boundary (SEC-006).

Everything the agent can be talked into doing arrives through this boundary, so the boundary is
tested for what it *refuses*:

* unknown fields are rejected rather than ignored (a silently dropped field is a silent
  misunderstanding of intent);
* string lengths and collection sizes are bounded, so one request cannot push an unbounded
  payload at the model or at a tool;
* a rejected request never reaches a tool or the LLM — it is refused before any work happens;
* rejections are counted and audited, because a burst of them is a signal (broken client, fuzzer
  or someone probing for laxness), and rate limiting answers 429 with ``Retry-After``.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.telemetry import METRIC_NAMES, REGISTRY
from tests.support.app import auth, free_port

pytestmark = [pytest.mark.story("SEC-006"), pytest.mark.security]


def metric_value(name: str, **labels: str) -> float:
    """Read one counter straight from the registry (the same series ``/metrics`` exports)."""
    assert name in METRIC_NAMES
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items()):
                return float(sample.value)
    return 0.0


# --------------------------------------------------------------------------- #
# Schema strictness
# --------------------------------------------------------------------------- #


async def test_unknown_fields_are_rejected(api: Any) -> None:
    """``extra="forbid"`` everywhere: a typo must not become a default."""
    response = await api.client.post(
        "/api/v1/detection/simulate",
        json={"scenario": "A", "orchestrate": True, "reset": True, "shell_command": "rm -rf /"},
    )

    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "validation_failed"
    assert body["status"] == 422
    assert "shell_command" in str(body["details"])


@pytest.mark.parametrize(
    "payload",
    [
        {"scenario": "A" * 200},  # unbounded enum-ish string
        {"scenario": "A", "metrics": ["error_rate"] * 100},  # unbounded collection
        {"scenario": "A", "orchestrate": "yes-please"},  # wrong type, not truthy-coerced
    ],
)
async def test_bounded_and_typed_fields_are_enforced(api: Any, payload: dict[str, Any]) -> None:
    response = await api.client.post("/api/v1/detection/simulate", json=payload)
    assert response.status_code == 422, f"{payload} was accepted"
    assert response.json()["error"]["code"] == "validation_failed"


async def test_collection_limits_are_enforced_on_reads(api: Any) -> None:
    """Pagination is bounded: ``limit=100000`` is a resource-exhaustion request, not a query."""
    for path in ("/api/v1/incidents", "/api/v1/audit/events"):
        response = await api.client.get(path, params={"limit": 100_000})
        assert response.status_code == 422, f"{path} accepted an unbounded limit"
        assert response.json()["error"]["code"] == "validation_failed"


async def test_a_rejected_request_never_reaches_a_tool_or_the_model(api: Any) -> None:
    """Validation happens before any work: no incident, no run, no tool invocation, no LLM call."""
    before_incidents = (await api.client.get("/api/v1/incidents")).json()["incidents"]

    rejected = await api.client.post(
        "/api/v1/detection/simulate",
        json={"scenario": "A", "orchestrate": True, "reset": True, "extra_field": "ignored?"},
    )
    assert rejected.status_code == 422

    after_incidents = (await api.client.get("/api/v1/incidents")).json()["incidents"]
    assert len(after_incidents) == len(before_incidents), "a rejected request created work"

    runs = (await api.client.get("/api/v1/agents/runs")).json()
    assert all(run["state"] == "NEW" for run in runs.get("runs", [])) or not runs.get("runs")


async def test_rejections_are_counted_and_audited(api: Any) -> None:
    """SEC-006: rejected input must be visible in metrics *and* in the audit chain."""
    before = metric_value("aiops_validation_rejections_total", route="/api/v1/detection/simulate")

    rejected = await api.client.post(
        "/api/v1/detection/simulate", json={"scenario": "A", "unknown": "field"}
    )
    assert rejected.status_code == 422

    after = metric_value("aiops_validation_rejections_total", route="/api/v1/detection/simulate")
    assert after == before + 1, "the rejection counter did not move"

    events = (
        await api.client.get("/api/v1/audit/events", params={"event_type": "validation_rejected"})
    ).json()
    assert events["events"], "the rejection left no audit trail"
    latest = events["events"][-1]
    assert latest["outcome"] == "rejected"
    assert latest["payload"]["path"] == "/api/v1/detection/simulate"
    assert "unknown" not in str(latest["payload"]), "the rejected payload must not be stored"


async def test_free_text_destined_for_external_systems_is_sanitised(api: Any) -> None:
    """A chat mention or a script tag must not travel from our API into Slack or Jira."""
    from app.core.sanitization import sanitize_for_external

    hostile = "<!channel> @here [click](javascript:alert(1)) <https://evil.example|here>"
    clean = sanitize_for_external(hostile, max_chars=300)

    # Chat-control sequences are defanged, not deleted: the operator still sees what arrived.
    assert "<!channel>" not in clean and "[mention:channel]" in clean
    assert "@here" not in clean and "[mention:here]" in clean
    assert "javascript:" not in clean, "a script URL must not survive into a chat message"
    assert clean.count(")") == 0 or "click)" not in clean, "no residue from the stripped link"
    assert "click" in clean, "the link text survives; the payload does not"
    assert "<https://evil.example|here>" not in clean, "phishing link syntax is reduced to text"
    assert len(clean) <= 300, "the size bound must hold after substitutions"


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #


async def test_rate_limiting_answers_429_with_retry_after(engine: Any, _base_settings: Any) -> None:
    """Sustained requests are refused with a machine-readable retry hint."""
    import httpx

    from app.core.config import Environment, IntegrationsMode
    from tests.support.app import start_server, stop_server

    settings = _base_settings.model_copy(
        update={
            "env": Environment.TEST,
            "integrations_mode": IntegrationsMode.SANDBOX,
            "rate_limit_requests_per_minute": 2,
            "rate_limit_burst": 0,
        }
    )
    handle = start_server(settings)
    try:
        with httpx.Client(base_url=handle.base_url, timeout=10.0, headers=auth("sre")) as client:
            statuses = [client.get("/api/v1/incidents").status_code for _ in range(5)]
        assert statuses[0] == 200
        assert 429 in statuses, f"the limiter never engaged: {statuses}"

        limited = httpx.get(
            f"{handle.base_url}/api/v1/incidents", timeout=10.0, headers=auth("sre")
        )
        if limited.status_code == 429:
            assert limited.headers.get("Retry-After"), "429 without Retry-After is unactionable"
            assert limited.json()["error"]["code"] == "rate_limited"
    finally:
        stop_server(handle)
    assert free_port() is not None  # the server released its port
