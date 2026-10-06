"""Distributed tracing (SRE-003).

The trace has to answer the question an operator asks at 03:00: *what did the agent actually do
for this incident, in what order, and how long did each step take?* That only works if one
incident produces **one** trace — spanning detection, evidence collection, the LLM call, policy,
the approval wait, execution and verification — even though those steps happen in different
requests (a human approves in one request, the executor resumes in the next).

Tracing is vendor-neutral OpenTelemetry with an in-memory exporter in tests, so nothing is
exported anywhere and the assertions are about the spans this service produced.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import Environment, IntegrationsMode, Settings
from app.core.tracing import (
    configure_tracing,
    current_trace_id,
    get_memory_exporter,
    incident_context,
    reset_tracing_for_tests,
    span,
)
from app.sandbox.simulator import RECOVERY_SETTLE_SECONDS
from tests.support.app import running_app

pytestmark = [pytest.mark.story("SRE-003"), pytest.mark.integration]


@pytest.fixture(autouse=True)
def _memory_exporter() -> Any:
    """A fresh provider per test, exporting into memory (no collector, no network)."""
    reset_tracing_for_tests()
    configure_tracing("aiops-agent", exporter="memory")
    yield get_memory_exporter()
    reset_tracing_for_tests()


@pytest.fixture
def settings(_base_settings: Settings) -> Settings:
    """The application under test runs with tracing on and a fast verification window.

    Overriding ``settings`` here (rather than mutating an object after the app was built) is what
    makes the *application* use these values: the container reads its settings at startup.
    """
    return _base_settings.model_copy(
        update={
            "env": Environment.TEST,
            "integrations_mode": IntegrationsMode.SANDBOX,
            "tracing_enabled": True,
            "tracing_exporter": "memory",
            # The simulator settles recovery over RECOVERY_SETTLE_SECONDS; a shorter window
            # would have the verifier observe a system that has not finished recovering and
            # (correctly) report FAILED.
            "verification_window_seconds": float(RECOVERY_SETTLE_SECONDS + 1),
            "verification_poll_interval_seconds": 0.5,
        }
    )


def _finished_spans(exporter: Any) -> list[Any]:
    return list(exporter.get_finished_spans())


def test_the_incident_id_determines_the_trace_id() -> None:
    """Two requests, one trace: the trace id is derived from the incident, not from the request."""
    first = incident_context("INC-TRACE-1")
    second = incident_context("INC-TRACE-1")
    other = incident_context("INC-TRACE-2")

    assert first.trace_id == second.trace_id
    assert first.trace_id != other.trace_id
    assert first.trace_id != 0


def test_spans_started_in_different_requests_join_the_same_trace() -> None:
    """The approval request and the resume request must land in the incident's trace."""
    with span("lifecycle.planning", incident_id="INC-TRACE-1"):
        first = current_trace_id()
    with span("lifecycle.execution", incident_id="INC-TRACE-1"):
        second = current_trace_id()

    assert first == second, "a random root span per request would split the incident in two"


async def test_a_whole_lifecycle_produces_one_trace_with_the_expected_stages(api: Any) -> None:
    """Detect → investigate → plan → approve → execute → verify, all in one trace."""
    exporter = get_memory_exporter()
    assert exporter is not None

    scenario = await api.client.post(
        "/api/v1/detection/simulate",
        json={"scenario": "A", "orchestrate": True, "reset": True},
    )
    assert scenario.status_code == 200
    incident_id = str(scenario.json()["incidents_created"][0])

    # Answer the approval gate and resume the run the way the console does. This is a *second*
    # request and must continue the same trace.
    pending = await api.client.get("/api/v1/approvals", params={"pending_only": "true"})
    approvals = [item for item in pending.json()["approvals"] if item["incident_id"] == incident_id]
    assert approvals, "scenario A must stop at the approval gate"
    decision = await api.client.post(
        f"/api/v1/approvals/{approvals[0]['id']}/decision",
        json={
            "decision": "APPROVED",
            "payload_hash": approvals[0]["payload_hash"],
            "reason": "tracing test",
        },
    )
    assert decision.status_code in {200, 201, 202}
    detail = await api.client.get(f"/api/v1/incidents/{incident_id}")
    run_id = str(detail.json()["run"]["id"])
    resume = await api.client.post(
        f"/api/v1/agents/runs/{run_id}/resume", json={"reason": "tracing"}
    )
    assert resume.status_code in {200, 201, 202}

    spans = _finished_spans(exporter)
    # FastAPI 0.142 emits its own request-scoped spans (``fastapi.*``, ``GET /api/v1/...``) with no
    # incident id. They are the HTTP view of the request; the incident's trace is the domain view,
    # and the two are deliberately not merged — a request is not an incident. Everything that
    # claims to belong to this incident must however live in exactly one trace.
    incident_spans = [item for item in spans if item.attributes.get("incident.id")]
    assert incident_spans, "the incident produced no spans at all"
    foreign = {
        str(item.attributes["incident.id"])
        for item in incident_spans
        if item.attributes["incident.id"] != incident_id
    }
    assert not foreign, f"spans for other incidents leaked into the export: {sorted(foreign)}"

    names = {item.name for item in incident_spans}
    trace_ids = {item.context.trace_id for item in incident_spans}

    assert len(trace_ids) == 1, f"one incident must produce one trace, got {len(trace_ids)}"
    assert trace_ids == {incident_context(incident_id).trace_id}, (
        "the trace id must be derivable from the incident id alone"
    )

    for expected in (
        "lifecycle.transition",
        "evidence.collect",
        "reasoning.llm",
        "lifecycle.planning",
        "lifecycle.execution",
        "lifecycle.verification",
        "policy.decision",
        "tool.invoke",
    ):
        assert expected in names, f"missing span {expected!r} in {sorted(names)}"

    # The approval gate is part of the story: the trace must show the run entering and leaving it.
    approval_states = {
        item.attributes.get("to.state")
        for item in incident_spans
        if item.name == "lifecycle.transition"
        and item.attributes.get("to.state") in {"WAITING_APPROVAL", "EXECUTING"}
    }
    assert approval_states == {"WAITING_APPROVAL", "EXECUTING"}, (
        f"the approval gate is not visible in the trace: {sorted(approval_states)}"
    )

    executed = [item for item in incident_spans if item.name == "tool.invoke"]
    assert executed and executed[0].attributes.get("tool.name") == "deployment.rollback_simulation"

    # The stages carry the ids a human needs to pivot between UI, logs and audit trail.
    with_ids = [item for item in incident_spans if item.attributes.get("run.id")]
    assert with_ids, "at least one span must carry the run id"
    for item in with_ids:
        assert item.attributes["incident.id"] == incident_id
        assert str(item.attributes["run.id"]) == run_id

    # Per-source evidence spans prove the fan-out is visible, not just the aggregate.
    source_spans = [item for item in incident_spans if item.name.startswith("evidence.")]
    assert len(source_spans) >= 3, (
        f"expected a span per source, got {[item.name for item in source_spans]}"
    )

    # Every span belongs to a finite, non-erroring lifecycle for the happy path.
    assert all(item.end_time is not None for item in incident_spans)


async def test_spans_do_not_leak_content_or_credentials(api: Any) -> None:
    """Attributes carry ids and counts — never evidence text, tokens or prompt bodies."""
    exporter = get_memory_exporter()
    assert exporter is not None

    scenario = await api.client.post(
        "/api/v1/detection/simulate",
        json={"scenario": "B", "orchestrate": True, "reset": True},
    )
    assert scenario.status_code == 200

    attributes = [value for item in _finished_spans(exporter) for value in item.attributes.values()]
    rendered = " ".join(str(value) for value in attributes)

    assert "ignore previous instructions" not in rendered.lower(), (
        "untrusted evidence text must not be copied into span attributes"
    )
    assert "Bearer" not in rendered and "test-token" not in rendered
    assert "api_key" not in rendered.lower()


async def test_tracing_can_be_disabled_without_changing_behaviour(
    engine: Any, _base_settings: Settings
) -> None:
    """``AIOPS_TRACING_ENABLED=false`` must be a no-op, not an error path.

    The application is built explicitly here so the tracing switch is the *only* difference from
    the traced run above, and the module fixture's provider is dropped first: otherwise the app
    would find a provider already installed (``configure_tracing`` installs only once) and the
    switch would be ignored.
    """
    reset_tracing_for_tests()
    off = _base_settings.model_copy(
        update={
            "env": Environment.TEST,
            "integrations_mode": IntegrationsMode.SANDBOX,
            "tracing_enabled": False,
            "tracing_exporter": "none",
            # The simulator settles recovery over RECOVERY_SETTLE_SECONDS; a shorter window
            # would have the verifier observe a system that has not finished recovering and
            # (correctly) report FAILED.
            "verification_window_seconds": float(RECOVERY_SETTLE_SECONDS + 1),
            "verification_poll_interval_seconds": 0.5,
        }
    )
    async with running_app(off) as handle:
        scenario = await handle.client.post(
            "/api/v1/detection/simulate",
            json={"scenario": "A", "orchestrate": True, "reset": True},
        )
        assert scenario.status_code == 200
        incident_id = str(scenario.json()["incidents_created"][0])
        # Behaviour is unchanged: the incident is detected, investigated, planned and parked at
        # the same approval gate, with the same evidence trail — the only difference is that no
        # span is recorded.
        detail = await handle.client.get(f"/api/v1/incidents/{incident_id}")
        assert detail.status_code == 200
        run = detail.json()["run"]
        assert run["state"] == "WAITING_APPROVAL"
        assert detail.json()["evidence_count"] > 0, "evidence is still collected and stored"
        pending = await handle.client.get("/api/v1/approvals", params={"pending_only": "true"})
        assert any(item["incident_id"] == incident_id for item in pending.json()["approvals"])

    assert get_memory_exporter() is None, "a disabled tracer must not install an exporter"
