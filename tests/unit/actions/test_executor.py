"""Action execution gates (OPS-062).

The executor is the only path from a proposal to a side effect, so these tests are written as
attempts to get a side effect through without clearing its gate: a tampered payload, a missing
approval, a stale approval, a timeout, and a retry. They use in-memory doubles for the
repositories so the *gate order* is what is under test — the repositories themselves are
exercised against real PostgreSQL in ``tests/integration/database``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any, cast

import pytest
from pydantic import BaseModel

from app.actions.executor import ActionExecutor
from app.core.config import Settings
from app.core.errors import ConflictError, ValidationFailed
from app.core.security import Actor, Permission
from app.core.telemetry import METRIC_NAMES, REGISTRY, TOOL_INVOCATIONS, UNSAFE_ACTION_ATTEMPTS
from app.domain.actions import (
    Action,
    ActionRequest,
    ExpectedState,
    canonical_hash,
    derive_idempotency_key,
)
from app.domain.enums import (
    ActionStatus,
    ApprovalDecision,
    AuditEventType,
    IncidentStatus,
    IncidentType,
    RiskLevel,
    Role,
    Severity,
    ToolOutcome,
)
from app.domain.incidents import Incident
from app.domain.tools import ToolResult, ToolSpec
from app.policy.engine import PolicyEngine
from app.tools.registry import (
    ToolContext,
    ToolDefinition,
    ToolHandler,
    ToolRegistry,
    build_default_registry,
)

pytestmark = [pytest.mark.story("OPS-062"), pytest.mark.unit]

ROLLBACK = "deployment.rollback_simulation"
PROBE = "tests.probe"

NOW = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.UTC)


class ProbeParams(BaseModel):
    note: str = "probe"


class Probe:
    """A controllable tool handler: counts calls, can be slow, and can fail on demand."""

    def __init__(self, *, delay: float = 0.0, failures: int = 0) -> None:
        self.delay = delay
        self.failures = failures
        self.calls = 0
        self.params: list[dict[str, Any]] = []

    async def __call__(self, params: ProbeParams, context: ToolContext) -> ToolResult:
        self.calls += 1
        self.params.append(params.model_dump())
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failures > 0:
            self.failures -= 1
            return ToolResult(
                tool_name=PROBE,
                success=False,
                outcome=ToolOutcome.FAILURE,
                error="transient upstream failure",
            )
        return ToolResult(
            tool_name=PROBE,
            success=True,
            outcome=ToolOutcome.SUCCESS,
            data={"note": params.note, "simulated": True},
            external_id="probe-1",
            simulated=True,
        )


def probe_definition(
    probe: Probe, *, timeout_seconds: float = 5.0, max_retries: int = 2, idempotent: bool = True
) -> ToolDefinition:
    return ToolDefinition(
        spec=ToolSpec(
            name=PROBE,
            description="test probe",
            permission=Permission.KNOWLEDGE_READ.value,
            risk=RiskLevel.LOW,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            idempotent=idempotent,
        ),
        params_model=ProbeParams,
        handler=cast(ToolHandler, probe),
    )


# --------------------------------------------------------------------------- #
# Repository doubles
# --------------------------------------------------------------------------- #


class ActionStore:
    def __init__(self) -> None:
        self.rows: dict[str, Action] = {}

    async def create(self, values: dict[str, Any]) -> Action:
        action = Action.model_validate({**values, "params": values.get("params", {})})
        self.rows[action.id] = action
        return action

    async def update(self, action_id: str, values: dict[str, Any]) -> Action | None:
        current = self.rows.get(action_id)
        if current is None:
            return None
        updated = Action.model_validate({**current.model_dump(), **values})
        self.rows[action_id] = updated
        return updated

    async def get(self, action_id: str) -> Action | None:
        return self.rows.get(action_id)


class ApprovalStore:
    def __init__(self) -> None:
        self.by_id: dict[str, Any] = {}
        self.binding: dict[str, bool] = {}

    async def get(self, approval_id: str) -> Any | None:
        return self.by_id.get(approval_id)

    async def get_for_action(self, action_id: str) -> Any | None:
        for approval in self.by_id.values():
            if approval.action_id == action_id:
                return approval
        return None

    async def verify_binding(self, approval_id: str, *, action: Action) -> bool:
        return self.binding.get(approval_id, True)


class Approval:
    def __init__(
        self,
        approval_id: str,
        action_id: str,
        *,
        decision: ApprovalDecision = ApprovalDecision.APPROVED,
        reason: str = "",
    ) -> None:
        self.id = approval_id
        self.action_id = action_id
        self.decision = decision
        self.reason = reason


class InvocationStore:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def record(self, values: dict[str, Any]) -> None:
        self.records.append(values)


class AuditStore:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def append(self, event: Any, **kwargs: Any) -> None:
        self.events.append((getattr(event, "value", str(event)), kwargs))

    def kinds(self) -> list[str]:
        return [name for name, _ in self.events]


class IdempotencyRecord:
    def __init__(self) -> None:
        self.request_hash = ""
        self.response: dict[str, Any] = {}
        self.external_id: str | None = None


class Idempotency:
    def __init__(self) -> None:
        self.rows: dict[str, IdempotencyRecord] = {}

    async def reserve(
        self, scope: str, key: str, request_hash: str
    ) -> tuple[IdempotencyRecord, bool]:
        existing = self.rows.get(key)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise ConflictError("different payload, same idempotency key")
            return existing, False
        record = IdempotencyRecord()
        record.request_hash = request_hash
        self.rows[key] = record
        return record, True

    async def complete(
        self, scope: str, key: str, *, external_id: str | None, response: dict[str, Any]
    ) -> None:
        record = self.rows[key]
        record.external_id = external_id
        record.response = response


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def probe() -> Probe:
    return Probe()


@pytest.fixture
def registry(probe: Probe) -> ToolRegistry:
    registry = build_default_registry()
    registry.register(probe_definition(probe))
    return registry


@pytest.fixture
def fast_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"action_timeout_seconds": 0.05, "action_retry_backoff_seconds": 0.001}
    )


@pytest.fixture
def make_gates(fast_settings: Settings) -> Any:
    """Build an executor over any registry, with in-memory doubles for every repository."""

    def _make(target_registry: ToolRegistry) -> dict[str, Any]:
        store = ActionStore()
        approvals = ApprovalStore()
        invocations = InvocationStore()
        audit = AuditStore()
        idempotency = Idempotency()
        executor = ActionExecutor(
            fast_settings,
            target_registry,
            PolicyEngine(target_registry, fast_settings),
            actions=store,  # type: ignore[arg-type]
            approvals=approvals,
            invocations=invocations,  # type: ignore[arg-type]
            audit=audit,  # type: ignore[arg-type]
            idempotency=idempotency,  # type: ignore[arg-type]
            integrations=object(),
        )
        return {
            "executor": executor,
            "actions": store,
            "approvals": approvals,
            "invocations": invocations,
            "audit": audit,
            "idempotency": idempotency,
            "settings": fast_settings,
        }

    return _make


@pytest.fixture
def gates(make_gates: Any, registry: ToolRegistry) -> dict[str, Any]:
    return cast(dict[str, Any], make_gates(registry))


@pytest.fixture
def sre() -> Actor:
    return Actor(actor_id="sre-1", role=Role.SRE, authenticated=True)


@pytest.fixture
def incident() -> Incident:
    return Incident(
        id="INC-EXEC-1",
        incident_type=IncidentType.API_ERROR_SPIKE,
        severity=Severity.SEV2,
        status=IncidentStatus.OPEN,
        title="error spike",
        service="checkout-service",
        metric="error_rate",
        dedup_key="dedup-exec",
        detected_at=NOW,
    )


def request(tool_name: str = ROLLBACK, **params: Any) -> ActionRequest:
    payload = {"target_release": "release-41", "reason": "error spike"}
    payload.update(params)
    return ActionRequest(
        incident_id="INC-EXEC-1",
        tool_name=tool_name,
        params=payload,
        rationale="regression after release-42",
        evidence_ids=["EV-1"],
        expected_state=ExpectedState(checks=[{"check": "release_active"}]),
    )


def prepared_action(gates: dict[str, Any]) -> Action:
    rows: dict[str, Action] = gates["actions"].rows
    action = next(iter(rows.values()), None)
    assert action is not None, "prepare() must have persisted an action"
    return action


# --------------------------------------------------------------------------- #
# Planning-time gates
# --------------------------------------------------------------------------- #


async def test_an_unregistered_tool_is_refused_and_audited(
    gates: dict[str, Any], sre: Actor
) -> None:
    action, decision, notes = await gates["executor"].prepare(
        request("shell.exec", command="rm -rf /"), actor=sre, incident=None
    )

    assert action is None, "an unregistered capability must never become an action row"
    assert decision.decision.value == "DENY"
    assert any("not registered" in note for note in notes)
    assert AuditEventType.UNKNOWN_TOOL_REQUESTED.value in gates["audit"].kinds()


async def test_a_viewer_cannot_prepare_a_write_action(gates: dict[str, Any]) -> None:
    viewer = Actor(actor_id="viewer-1", role=Role.VIEWER, authenticated=True)
    action, decision, notes = await gates["executor"].prepare(
        request("jira.create_incident", incident_id="INC-EXEC-1", summary="spike"), actor=viewer
    )
    assert action is None
    assert decision.decision.value == "DENY"
    assert any("denied" in note for note in notes)


async def test_a_high_risk_proposal_is_parked_for_approval(
    gates: dict[str, Any], sre: Actor, incident: Incident
) -> None:
    action, decision, _notes = await gates["executor"].prepare(
        request(), actor=sre, incident=incident, run_id="RUN-1"
    )

    assert action is not None
    assert decision.decision.value == "REQUIRE_APPROVAL"
    assert action.status is ActionStatus.AWAITING_APPROVAL
    assert action.requires_approval is True
    assert action.risk is RiskLevel.HIGH
    assert action.run_id == "RUN-1"
    assert action.canonical_hash == canonical_hash(action.payload)
    assert "high_risk" in decision.reason
    assert AuditEventType.ACTION_PROPOSED.value in gates["audit"].kinds()


async def test_incident_scope_is_filled_in_deterministically(
    gates: dict[str, Any], sre: Actor
) -> None:
    action, _, notes = await gates["executor"].prepare(
        request("jira.create_incident", summary="error spike", incident_id=""),
        actor=sre,
    )
    assert action is not None
    assert action.params["incident_id"] == "INC-EXEC-1"
    assert any("incident_id" in note for note in notes)


async def test_an_action_pointing_at_another_incident_is_denied(
    gates: dict[str, Any], sre: Actor, incident: Incident
) -> None:
    """An injected "roll back the other service" must not cross the incident boundary."""
    action, decision, _ = await gates["executor"].prepare(
        request("jira.create_incident", incident_id="INC-OTHER", summary="spike"),
        actor=sre,
        incident=incident,
    )
    assert action is None
    assert decision.decision.value == "DENY"
    assert "cross_incident_action" in decision.violations


# --------------------------------------------------------------------------- #
# Execution-time gates
# --------------------------------------------------------------------------- #


async def test_a_tampered_payload_is_never_executed(
    gates: dict[str, Any], sre: Actor, probe: Probe
) -> None:
    action = await gates["actions"].create(
        {
            "id": "ACT-TAMPER",
            "incident_id": "INC-EXEC-1",
            "tool_name": PROBE,
            "risk": RiskLevel.LOW.value,
            "permission": Permission.KNOWLEDGE_READ.value,
            "params": {"note": "original"},
            "canonical_hash": canonical_hash({"tool_name": PROBE, "params": {"note": "edited"}}),
            "idempotency_key": "idem-tamper",
        }
    )

    outcome = await gates["executor"].execute(action, actor=sre)

    assert outcome.executed is False
    assert "hash" in outcome.reason
    assert probe.calls == 0, "a tampered payload must not reach the tool"
    assert gates["actions"].rows["ACT-TAMPER"].status is ActionStatus.FAILED


async def test_a_high_risk_action_without_an_approval_waits(
    gates: dict[str, Any], sre: Actor, incident: Incident
) -> None:
    action, _, _ = await gates["executor"].prepare(request(), actor=sre, incident=incident)

    outcome = await gates["executor"].execute(action, actor=sre, incident=incident)

    assert outcome.awaiting_approval is True
    assert outcome.executed is False
    assert gates["invocations"].records == []
    assert gates["actions"].rows[action.id].status is ActionStatus.AWAITING_APPROVAL


async def test_a_rejected_approval_does_not_execute(
    gates: dict[str, Any], sre: Actor, incident: Incident
) -> None:
    action, _, _ = await gates["executor"].prepare(request(), actor=sre, incident=incident)
    gates["approvals"].by_id["APR-1"] = Approval(
        action.id, action.id, decision=ApprovalDecision.REJECTED, reason="too risky"
    )

    outcome = await gates["executor"].execute(action, actor=sre, approval_id="APR-1")

    assert outcome.executed is False
    assert "rejected" in outcome.reason
    assert gates["actions"].rows[action.id].status is ActionStatus.REJECTED
    assert AuditEventType.APPROVAL_REJECTED.value in gates["audit"].kinds()


async def test_a_stale_approval_is_invalidated_rather_than_used(
    gates: dict[str, Any], sre: Actor, incident: Incident
) -> None:
    action, _, _ = await gates["executor"].prepare(request(), actor=sre, incident=incident)
    gates["approvals"].by_id["APR-1"] = Approval("APR-1", action.id)
    gates["approvals"].binding["APR-1"] = False

    outcome = await gates["executor"].execute(action, actor=sre, approval_id="APR-1")

    assert outcome.executed is False
    assert "expired" in outcome.reason or "does not match" in outcome.reason
    assert AuditEventType.APPROVAL_INVALIDATED.value in gates["audit"].kinds()


async def test_a_bound_approval_executes_the_action_once(
    gates: dict[str, Any], sre: Actor, probe: Probe
) -> None:
    action = await gates["actions"].create(
        {
            "id": "ACT-RUN",
            "incident_id": "INC-EXEC-1",
            "tool_name": PROBE,
            "risk": RiskLevel.LOW.value,
            "permission": Permission.KNOWLEDGE_READ.value,
            "params": {"note": "go"},
            "canonical_hash": canonical_hash({"tool_name": PROBE, "params": {"note": "go"}}),
            "idempotency_key": derive_idempotency_key("INC-EXEC-1", PROBE, {"note": "go"}),
        }
    )

    outcome = await gates["executor"].execute(action, actor=sre)

    assert outcome.executed is True
    assert outcome.ok is True
    assert probe.calls == 1
    assert gates["actions"].rows["ACT-RUN"].status is ActionStatus.SUCCEEDED
    assert gates["invocations"].records[0]["outcome"] == ToolOutcome.SIMULATED.value
    assert AuditEventType.ACTION_EXECUTED.value in gates["audit"].kinds()


async def test_the_same_intent_replays_instead_of_running_twice(
    gates: dict[str, Any], sre: Actor, probe: Probe
) -> None:
    first = await gates["actions"].create(
        {
            "id": "ACT-R1",
            "incident_id": "INC-EXEC-1",
            "tool_name": PROBE,
            "risk": RiskLevel.LOW.value,
            "permission": Permission.KNOWLEDGE_READ.value,
            "params": {"note": "go"},
            "canonical_hash": canonical_hash({"tool_name": PROBE, "params": {"note": "go"}}),
            "idempotency_key": derive_idempotency_key("INC-EXEC-1", PROBE, {"note": "go"}),
        }
    )
    await gates["executor"].execute(first, actor=sre)

    # A second attempt with a fresh action row but the same intent must replay the result.
    second = await gates["actions"].create(
        {
            "id": "ACT-R2",
            "incident_id": "INC-EXEC-1",
            "tool_name": PROBE,
            "risk": RiskLevel.LOW.value,
            "permission": Permission.KNOWLEDGE_READ.value,
            "params": {"note": "go"},
            "canonical_hash": canonical_hash({"tool_name": PROBE, "params": {"note": "go"}}),
            "idempotency_key": derive_idempotency_key("INC-EXEC-1", PROBE, {"note": "go"}),
        }
    )
    outcome = await gates["executor"].execute(second, actor=sre)

    assert outcome.replayed is True
    assert probe.calls == 1, "the side effect must happen exactly once"
    assert AuditEventType.ACTION_REPLAYED.value in gates["audit"].kinds()
    assert outcome.result is not None and outcome.result.replayed is True


async def test_a_different_payload_under_the_same_key_is_a_conflict(
    gates: dict[str, Any], sre: Actor, probe: Probe
) -> None:
    key = derive_idempotency_key("INC-EXEC-1", PROBE, {"note": "go"})
    first = await gates["actions"].create(
        {
            "id": "ACT-C1",
            "incident_id": "INC-EXEC-1",
            "tool_name": PROBE,
            "risk": RiskLevel.LOW.value,
            "permission": Permission.KNOWLEDGE_READ.value,
            "params": {"note": "go"},
            "canonical_hash": canonical_hash({"tool_name": PROBE, "params": {"note": "go"}}),
            "idempotency_key": key,
        }
    )
    await gates["executor"].execute(first, actor=sre)

    changed = await gates["actions"].create(
        {
            "id": "ACT-C2",
            "incident_id": "INC-EXEC-1",
            "tool_name": PROBE,
            "risk": RiskLevel.LOW.value,
            "permission": Permission.KNOWLEDGE_READ.value,
            "params": {"note": "go"},
            "canonical_hash": canonical_hash({"tool_name": PROBE, "params": {"note": "go"}}),
            "idempotency_key": key,
        }
    )
    gates["idempotency"].rows[key].request_hash = "different"

    with pytest.raises(ConflictError):
        await gates["executor"].execute(changed, actor=sre)
    assert probe.calls == 1


# --------------------------------------------------------------------------- #
# Timeouts and retries
# --------------------------------------------------------------------------- #


async def test_a_timeout_fails_the_action_with_a_typed_reason(make_gates: Any, sre: Actor) -> None:
    slow = Probe(delay=0.2)
    gates = make_gates(ToolRegistry([probe_definition(slow, timeout_seconds=0.01, max_retries=0)]))

    action = await gates["actions"].create(
        {
            "id": "ACT-SLOW",
            "incident_id": "INC-EXEC-1",
            "tool_name": PROBE,
            "risk": RiskLevel.LOW.value,
            "permission": Permission.KNOWLEDGE_READ.value,
            "params": {"note": "slow"},
            "canonical_hash": canonical_hash({"tool_name": PROBE, "params": {"note": "slow"}}),
            "idempotency_key": "idem-slow",
        }
    )

    outcome = await gates["executor"].execute(action, actor=sre)

    assert outcome.executed is False
    assert gates["actions"].rows["ACT-SLOW"].status is ActionStatus.FAILED
    assert gates["invocations"].records[-1]["outcome"] == ToolOutcome.TIMEOUT.value
    assert AuditEventType.ACTION_FAILED.value in gates["audit"].kinds()


async def test_a_retryable_failure_retries_an_idempotent_tool(make_gates: Any, sre: Actor) -> None:
    flaky = Probe(failures=1)
    gates = make_gates(ToolRegistry([probe_definition(flaky, max_retries=2, idempotent=True)]))

    action = await gates["actions"].create(
        {
            "id": "ACT-FLAKY",
            "incident_id": "INC-EXEC-1",
            "tool_name": PROBE,
            "risk": RiskLevel.LOW.value,
            "permission": Permission.KNOWLEDGE_READ.value,
            "params": {"note": "flaky"},
            "canonical_hash": canonical_hash({"tool_name": PROBE, "params": {"note": "flaky"}}),
            "idempotency_key": "idem-flaky",
        }
    )

    outcome = await gates["executor"].execute(action, actor=sre)

    assert outcome.executed is True
    assert flaky.calls == 2, "a transient failure is retried once"
    assert gates["actions"].rows["ACT-FLAKY"].status is ActionStatus.SUCCEEDED


async def test_a_non_idempotent_tool_is_never_retried(make_gates: Any, sre: Actor) -> None:
    """Retrying a non-idempotent side effect is how one rollback becomes two."""
    flaky = Probe(failures=1)
    gates = make_gates(ToolRegistry([probe_definition(flaky, max_retries=5, idempotent=False)]))

    action = await gates["actions"].create(
        {
            "id": "ACT-ONCE",
            "incident_id": "INC-EXEC-1",
            "tool_name": PROBE,
            "risk": RiskLevel.LOW.value,
            "permission": Permission.KNOWLEDGE_READ.value,
            "params": {"note": "once"},
            "canonical_hash": canonical_hash({"tool_name": PROBE, "params": {"note": "once"}}),
            "idempotency_key": "idem-once",
        }
    )

    outcome = await gates["executor"].execute(action, actor=sre)

    assert outcome.executed is False
    assert flaky.calls == 1


async def test_invalid_parameters_fail_before_the_tool_runs(
    gates: dict[str, Any], sre: Actor, probe: Probe
) -> None:
    action = await gates["actions"].create(
        {
            "id": "ACT-BAD",
            "incident_id": "INC-EXEC-1",
            "tool_name": PROBE,
            "risk": RiskLevel.LOW.value,
            "permission": Permission.KNOWLEDGE_READ.value,
            "params": {"note": 12345},
            "canonical_hash": canonical_hash({"tool_name": PROBE, "params": {"note": 12345}}),
            "idempotency_key": "idem-bad",
        }
    )

    outcome = await gates["executor"].execute(action, actor=sre)

    assert outcome.executed is False
    assert probe.calls == 0
    assert gates["invocations"].records[-1]["outcome"] == ToolOutcome.INVALID_INPUT.value


async def test_every_execution_records_a_duration_and_an_actor(
    gates: dict[str, Any], sre: Actor
) -> None:
    action = await gates["actions"].create(
        {
            "id": "ACT-META",
            "incident_id": "INC-EXEC-1",
            "tool_name": PROBE,
            "risk": RiskLevel.LOW.value,
            "permission": Permission.KNOWLEDGE_READ.value,
            "params": {"note": "meta"},
            "canonical_hash": canonical_hash({"tool_name": PROBE, "params": {"note": "meta"}}),
            "idempotency_key": "idem-meta",
        }
    )
    await gates["executor"].execute(action, actor=sre)

    record = gates["invocations"].records[-1]
    assert record["actor"] == "sre-1"
    assert record["duration_ms"] >= 0.0
    assert record["tool_name"] == PROBE


async def test_validation_failure_is_raised_from_the_registry_not_the_tool(
    registry: ToolRegistry, fast_settings: Settings
) -> None:
    """The registry validates parameters; a handler never sees a malformed request."""
    with pytest.raises(ValidationFailed):
        await registry.get(PROBE).invoke(
            {"note": 1},
            ToolContext(settings=fast_settings, integrations=object(), actor="test"),  # type: ignore[arg-type]
        )


def test_execution_counters_are_declared_and_labelled() -> None:
    """The metrics the runbook reads must exist and carry the labels it queries."""
    declared = {
        name[: -len("_total")] if name.endswith("_total") else name for name in METRIC_NAMES
    }
    registered = {family.name for family in REGISTRY.collect()}
    assert "aiops_tool_invocations" in declared & registered
    assert "aiops_unsafe_action_attempts" in declared & registered
    assert TOOL_INVOCATIONS._labelnames == ("tool", "risk", "outcome")
    assert "outcome" in UNSAFE_ACTION_ATTEMPTS._labelnames
