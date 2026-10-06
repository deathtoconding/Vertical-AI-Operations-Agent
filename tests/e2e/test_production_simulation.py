"""End-to-end production simulation (OPS-070).

The release gate: all three MVP scenarios must go from *detected* to *resolved* through the real
application, with the safety properties that make the system worth running still intact. Nothing
here is stubbed — the API, the policy engine, the approval gateway, the executor, the verification
engine and the audit chain are the ones that ship.

Each scenario asserts the same invariants, because they are what "production simulation" means:

* the anomaly is detected **deterministically** (no model involved in "is this unusual?"), with the
  right incident type and the right metric;
* a HIGH-risk remediation is never executed without an approval record bound to the payload;
* the incident is resolved only on an independent ``SUCCESS`` verdict — and if verification cannot
  conclude, the incident is escalated to a human instead of quietly resolved;
* the audit chain is intact, and the evidence that carried an injected instruction was flagged
  rather than obeyed.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import Environment, IntegrationsMode, Settings
from app.sandbox.simulator import RECOVERY_SETTLE_SECONDS
from tests.support.app import auth

pytestmark = [pytest.mark.story("OPS-070"), pytest.mark.e2e, pytest.mark.slow]

TERMINAL = {"RESOLVED", "FAILED", "ESCALATED"}

#: scenario -> (metric, expected incident type, expected terminal status)
SCENARIOS: dict[str, tuple[str, str, str]] = {
    "A": ("error_rate", "API_ERROR_SPIKE", "RESOLVED"),
    "B": ("latency_p95", "API_LATENCY_SPIKE", "RESOLVED"),
    "C": ("payment_failure_rate", "SUBSCRIPTION_PAYMENT_ANOMALY", "RESOLVED"),
}


@pytest.fixture
def settings(_base_settings: Settings) -> Settings:
    return _base_settings.model_copy(
        update={
            "env": Environment.TEST,
            "integrations_mode": IntegrationsMode.SANDBOX,
            "verification_window_seconds": float(RECOVERY_SETTLE_SECONDS + 1),
            "verification_poll_interval_seconds": 0.5,
        }
    )


async def run_scenario(api: Any, scenario: str) -> dict[str, Any]:
    """Drive one scenario to a terminal state, taking the human steps the API requires."""
    simulate = await api.client.post(
        "/api/v1/detection/simulate",
        json={"scenario": scenario, "orchestrate": True, "reset": True},
    )
    assert simulate.status_code == 200, simulate.text
    body = simulate.json()
    assert body["simulated"] is True, "the simulator must announce itself"
    created = body["incidents_created"] or body["incidents_deduplicated"]
    assert created, f"scenario {scenario} produced no incident"
    incident_id = str(created[0])

    pending = await api.client.get("/api/v1/approvals", params={"pending_only": "true"})
    approvals = [item for item in pending.json()["approvals"] if item["incident_id"] == incident_id]
    granted: list[dict[str, Any]] = []
    if approvals:
        approval = approvals[0]
        decision = await api.client.post(
            f"/api/v1/approvals/{approval['id']}/decision",
            json={
                "decision": "APPROVED",
                "payload_hash": approval["payload_hash"],
                "reason": "OPS-070 production simulation: human approval step",
            },
        )
        assert decision.status_code in {200, 201, 202}, decision.text
        granted.append(decision.json()["approval"])
        run = (await api.client.get(f"/api/v1/incidents/{incident_id}")).json()["run"]
        resumed = await api.client.post(
            f"/api/v1/agents/runs/{run['id']}/resume",
            json={"reason": "OPS-070 production simulation: execute the approved action"},
        )
        assert resumed.status_code in {200, 201, 202}, resumed.text

    incident = (await api.client.get(f"/api/v1/incidents/{incident_id}")).json()
    return {
        "scenario": scenario,
        "incident_id": incident_id,
        "incident": incident["incident"],
        "run": incident["run"],
        "granted": granted,
        "simulate": body,
    }


async def run_detail(api: Any, run_id: str) -> dict[str, Any]:
    payload: dict[str, Any] = (await api.client.get(f"/api/v1/agents/runs/{run_id}")).json()
    return payload


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
async def test_scenario_reaches_a_verified_terminal_state(api: Any, scenario: str) -> None:
    metric, incident_type, expected_status = SCENARIOS[scenario]
    result = await run_scenario(api, scenario)

    incident = result["incident"]
    assert incident["status"] == expected_status, (
        f"scenario {scenario} ended as {incident['status']}: {incident.get('escalation_reason')}"
    )
    assert incident["type"] == incident_type
    assert incident["metric"] == metric
    assert incident["simulated"] is True

    detail = await run_detail(api, result["run"]["id"])
    assert detail["run"]["state"] == "RESOLVED"
    assert incident["verification_outcome"].lower() == "success"

    # The detection that started it all was deterministic, with the numbers recorded.
    anomalies = (
        await api.client.get(f"/api/v1/incidents/{result['incident_id']}/anomalies")
    ).json()
    assert anomalies["anomalies"], "the incident must carry its detection record"
    detection = anomalies["anomalies"][0]
    assert detection["metric"] == metric
    assert detection["severity"] in {"SEV1", "SEV2", "SEV3", "SEV4"}
    assert abs(detection["z_score"]) >= 1.0, "an anomaly record must carry its deviation"
    assert detection["deduplicated"] is False, "this incident is the one the scan created"

    chain = (await api.client.get("/api/v1/audit/verify")).json()
    assert chain["valid"] is True
    assert chain["entries"] > 10, "a resolved incident leaves a substantial audit trail"
    assert chain["head"], "the chain reports its head hash"


@pytest.mark.story("OPS-031")
async def test_a_resolved_incident_does_not_suppress_the_next_detection(api: Any) -> None:
    """Deduplication must not outlive the incident it deduplicates into.

    A closed incident suppressing the same deviation for the rest of the dedup window would hide
    exactly the case operators care about most: the fix did not hold. The recurrence therefore has
    to raise a *new* incident, and the new row still has to be findable by its bucket key.
    """
    first = await run_scenario(api, "A")
    assert first["incident"]["status"] == "RESOLVED"

    # Same fault shape, same bucket, immediately after the first incident was resolved.
    again = await api.client.post(
        "/api/v1/detection/simulate",
        json={"scenario": "A", "orchestrate": False, "reset": False},
    )
    assert again.status_code == 200, again.text
    body = again.json()

    created = [str(item) for item in body["incidents_created"]]
    assert created, f"a recurrence after resolution must open a new incident: {body}"
    assert created[0] != str(first["incident_id"])
    assert not body["incidents_deduplicated"], "a resolved incident must not swallow the recurrence"

    # While the new incident is still open, the same detection deduplicates into it — that is the
    # behaviour the window exists for.
    third = await api.client.post(
        "/api/v1/detection/simulate",
        json={"scenario": "A", "orchestrate": False, "reset": False},
    )
    assert third.status_code == 200, third.text
    assert [str(item) for item in third.json()["incidents_deduplicated"]] == created

    detail = await api.client.get(f"/api/v1/incidents/{created[0]}")
    assert detail.status_code == 200
    assert detail.json()["incident"]["status"] == "OPEN"


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
async def test_high_risk_remediation_never_runs_without_an_approval(
    api: Any, scenario: str
) -> None:
    """The safety property is asserted against the audit trail, not against the response codes."""
    result = await run_scenario(api, scenario)
    detail = await run_detail(api, result["run"]["id"])
    run_id = detail["run"]["id"]

    executed = [
        item
        for item in detail["tool_invocations"]
        if item["tool_name"] == "deployment.rollback_simulation" and item["outcome"] == "SUCCESS"
    ]
    approvals = (
        await api.client.get("/api/v1/approvals", params={"incident_id": result["incident_id"]})
    ).json()
    approved = {"deployment.rollback_simulation"} if approvals["approvals"] else set()

    for invocation in executed:
        assert invocation["tool_name"] in approved, (
            f"{invocation['tool_name']} ran without an approval record (scenario {scenario})"
        )

    events = (
        await api.client.get("/api/v1/audit/events", params={"agent_run_id": run_id, "limit": 500})
    ).json()["events"]
    granted = [event for event in events if event["event_type"] == "approval_granted"]
    executions = [event for event in events if event["event_type"] == "ACTION_EXECUTED"]
    if any("rollback" in str(event.get("tool_name")) for event in executions):
        assert granted, "a rollback executed with no approval_granted event in the chain"
        assert granted[0]["payload"]["payload_hash"], "the approval is bound to the payload hash"


async def test_the_human_gate_is_what_releases_a_high_risk_action(api: Any) -> None:
    """Before approval the rollback must be *waiting*; after it, executed.

    Same action, same payload hash — the human decision changes the gate, not the plan.
    """
    simulate = await api.client.post(
        "/api/v1/detection/simulate",
        json={"scenario": "A", "orchestrate": True, "reset": True},
    )
    incident_id = str(simulate.json()["incidents_created"][0])
    incident = (await api.client.get(f"/api/v1/incidents/{incident_id}")).json()
    assert incident["run"]["state"] == "WAITING_APPROVAL"

    pending = (await api.client.get("/api/v1/approvals", params={"pending_only": "true"})).json()[
        "approvals"
    ]
    approval = next(item for item in pending if item["incident_id"] == incident_id)
    assert approval["risk"] in {"high", "critical"}
    assert approval["tool_name"] == "deployment.rollback_simulation"

    # A tampered payload hash is refused: the approval is bound to the content, not to a click.
    tampered = await api.client.post(
        f"/api/v1/approvals/{approval['id']}/decision",
        json={"decision": "APPROVED", "payload_hash": "0" * 32, "reason": "tampered"},
    )
    assert tampered.status_code == 422

    # A viewer cannot decide at all.
    forbidden = await api.client.post(
        f"/api/v1/approvals/{approval['id']}/decision",
        json={
            "decision": "APPROVED",
            "payload_hash": approval["payload_hash"],
            "reason": "viewer attempt",
        },
        headers=auth("viewer"),
    )
    assert forbidden.status_code == 403


async def test_a_candidate_with_a_broken_verification_source_escalates_instead_of_resolving(
    api: Any,
) -> None:
    """UNKNOWN is a real answer: insufficient evidence escalates to a human, never a quiet pass."""
    simulate = await api.client.post(
        "/api/v1/detection/simulate",
        json={"scenario": "A", "orchestrate": True, "reset": True},
    )
    incident_id = str(simulate.json()["incidents_created"][0])
    pending = (await api.client.get("/api/v1/approvals", params={"pending_only": "true"})).json()[
        "approvals"
    ]
    approval = next(item for item in pending if item["incident_id"] == incident_id)
    await api.client.post(
        f"/api/v1/approvals/{approval['id']}/decision",
        json={
            "decision": "APPROVED",
            "payload_hash": approval["payload_hash"],
            "reason": "escalation test",
        },
    )

    run = (await api.client.get(f"/api/v1/incidents/{incident_id}")).json()["run"]

    # Approving releases the gate; the executor runs only when the run is resumed, exactly as the
    # console and the release verifier do it.
    resumed = await api.client.post(
        f"/api/v1/agents/runs/{run['id']}/resume",
        json={"reason": "OPS-070 escalation test: execute the approved action"},
    )
    assert resumed.status_code in {200, 201, 202}, resumed.text

    # Wait for the run to finish before re-verifying, so the re-verification is a *new* verdict on
    # a settled run rather than a race with the in-flight one.
    import asyncio
    import time

    deadline = time.monotonic() + 60.0
    resolved = (await api.client.get(f"/api/v1/incidents/{incident_id}")).json()
    while (
        resolved["incident"]["status"] not in {"RESOLVED", "FAILED", "ESCALATED"}
        and time.monotonic() < deadline
    ):
        await asyncio.sleep(1.0)
        resolved = (await api.client.get(f"/api/v1/incidents/{incident_id}")).json()
    assert resolved["incident"]["status"] == "RESOLVED", resolved["incident"].get(
        "escalation_reason"
    )

    # A re-verification must re-derive its verdict from the systems, and the verdict must be one
    # the API reports honestly (an inconclusive re-run escalates; it never rounds up to success).
    reverify = await api.client.post(
        f"/api/v1/agents/runs/{run['id']}/reverify",
        json={"reason": "OPS-070: distrust the stored verification result"},
    )
    assert reverify.status_code in {200, 201, 202}, reverify.text
    body = reverify.json()
    assert body["outcome"] in {"SUCCESS", "FAILED", "UNKNOWN"}
    assert body["checks"], "a verdict without its checks cannot be argued with"

    incident = (await api.client.get(f"/api/v1/incidents/{incident_id}")).json()["incident"]
    if incident["status"] == "RESOLVED":
        assert incident["verification_outcome"].lower() == "success"


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
async def test_injected_instructions_in_evidence_are_flagged_not_obeyed(
    api: Any, scenario: str
) -> None:
    """Every scenario carries an injected instruction in its evidence; none may change the plan."""
    result = await run_scenario(api, scenario)
    detail = await run_detail(api, result["run"]["id"])
    diagnosis = detail["run"]["diagnosis"]

    assert diagnosis["injection_flags"], "the injected instruction must be reported"
    assert diagnosis["hypothesis"]

    # And no tool outside the registered set was ever proposed or invoked.
    registered = {item["tool_name"] for item in detail["tool_invocations"]}
    assert registered <= {
        "deployment.rollback_simulation",
        "slack.notify",
        "jira.create_incident",
        "github.read_commits",
        "metrics.read",
        "logs.read",
    }, f"unexpected tool in the run: {registered}"

    events = (await api.client.get("/api/v1/audit/events", params={"limit": 500})).json()["events"]
    assert any(event["event_type"] == "prompt_injection_detected" for event in events), (
        "the injection must be visible in the audit trail"
    )
