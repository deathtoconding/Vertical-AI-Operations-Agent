"""Independent verification (OPS-063 / ADR-0004).

The rule under test is the one that makes the whole agent safe to run: **an HTTP 200 is not
evidence that anything improved**. The executor may report success; if the world does not match
the expectation declared before execution, the outcome is ``FAILED``. Equally, if the world
cannot be read, the outcome is ``UNKNOWN`` — never a quiet pass.

The integration facade is the real sandbox facade (a system that answers, not a stub that
agrees), and the repositories are faithful in-memory doubles: their SQL behaviour has its own
PostgreSQL tests in ``tests/integration/database/test_persistence.py``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest

from app.core.config import Settings
from app.core.errors import IntegrationUnavailable
from app.domain.actions import Action
from app.domain.enums import (
    ActionStatus,
    AgentState,
    AuditEventType,
    RiskLevel,
    Severity,
    VerificationOutcome,
)
from app.domain.verification import CheckResult, VerificationResult, aggregate_outcomes
from app.integrations.facade import build_integrations
from app.sandbox.simulator import get_sandbox
from app.verification.checks import MIN_SAMPLES_FOR_CONCLUSION, run_check
from app.verification.engine import VerificationEngine
from app.verification.outcomes import outcome_guidance, resolves_incident, summarise

pytestmark = [pytest.mark.story("OPS-063"), pytest.mark.unit]

NOW = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.UTC)


# --------------------------------------------------------------------------- #
# In-memory doubles for the persistence layer
# --------------------------------------------------------------------------- #


class FakeVerificationRepository:
    def __init__(self) -> None:
        self.records: list[VerificationResult] = []

    async def create(self, values: dict[str, Any]) -> VerificationResult:
        record = VerificationResult.model_validate({**values, "created_at": NOW, "updated_at": NOW})
        self.records.append(record)
        return record

    async def latest_for_incident(self, incident_id: str) -> VerificationResult | None:
        matches = [item for item in self.records if item.incident_id == incident_id]
        return matches[-1] if matches else None


class FakeActionRepository:
    def __init__(self) -> None:
        self.actions: dict[str, Action] = {}

    def add(self, action: Action) -> Action:
        self.actions[action.id] = action
        return action

    async def get(self, action_id: str) -> Action | None:
        return self.actions.get(action_id)


class FakeAuditRepository:
    """Records the audit trail so a test can assert that an inconclusive outcome is visible."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def append(self, event_type: AuditEventType, **kwargs: Any) -> None:
        self.events.append({"event_type": event_type, **kwargs})


@pytest.fixture(autouse=True)
def _sandbox_scenario_a() -> Iterator[None]:
    """Start from the simulated regression (release-42 active, release-41 as the target)."""
    sandbox = get_sandbox()
    sandbox.reset(scenario="A")
    yield
    sandbox.reset(scenario="normal")


@pytest.fixture
def integrations(settings: Settings) -> Any:
    return build_integrations(settings)


@pytest.fixture
def engine(
    settings: Settings, integrations: Any
) -> tuple[
    VerificationEngine, FakeActionRepository, FakeAuditRepository, FakeVerificationRepository
]:
    actions = FakeActionRepository()
    audit = FakeAuditRepository()
    verifications = FakeVerificationRepository()
    # The settle window is production behaviour (recovery is not instantaneous); the unit test
    # does not pay it, because the window's *effect* is what matters, not the sleeping.
    fast = settings.model_copy(update={"verification_window_seconds": 0.0})
    return (
        VerificationEngine(
            fast,
            integrations,
            verifications=verifications,  # type: ignore[arg-type]
            actions=actions,  # type: ignore[arg-type]
            audit=audit,  # type: ignore[arg-type]
        ),
        actions,
        audit,
        verifications,
    )


def action(tool_name: str, **overrides: Any) -> Action:
    values: dict[str, Any] = {
        "id": "ACT-VERIFY-1",
        "incident_id": "INC-VERIFY-1",
        "run_id": "RUN-VERIFY-1",
        "tool_name": tool_name,
        "risk": RiskLevel.HIGH,
        "permission": "deployment.write",
        "params": {"target_release": "release-41"},
        "canonical_hash": "hash",
        "idempotency_key": "key",
        "status": ActionStatus.SUCCEEDED,
        "attempts": 1,
        "result": {"http_status": 200, "ok": True, "simulated": True},
        "expected_state": {
            "checks": [{"check": "release_active"}],
            "target_release": "release-41",
        },
    }
    values.update(overrides)
    return Action.model_validate(values)


# --------------------------------------------------------------------------- #
# The central rule
# --------------------------------------------------------------------------- #


async def test_a_successful_executor_result_does_not_verify_anything(
    engine: tuple[Any, FakeActionRepository, FakeAuditRepository, FakeVerificationRepository],
) -> None:
    """A tool that returned HTTP 200 while nothing changed must verify as FAILED.

    The executor "succeeded" and said so; the release is still the bad one because the
    rollback was a no-op. Verification reads the deployment system, not the tool's answer.
    """
    engine_handle, actions, _audit, _records = engine
    action_row = action("deployment.rollback_simulation")
    actions.add(action_row)

    result = await engine_handle.verify_action(action_row.id, wait_seconds=0.0)

    assert result.outcome is VerificationOutcome.FAILED, (
        "the executor reported success but the observed release did not change"
    )
    check = next(item for item in result.checks if item["check"] == "release_active")
    assert check["outcome"] == "FAILED"
    assert "release-42" in check["reason"]
    assert result.reason.startswith("failed")


async def test_verification_reads_the_system_after_the_rollback_actually_happens(
    engine: tuple[Any, FakeActionRepository, FakeAuditRepository, FakeVerificationRepository],
) -> None:
    """The same declared expectation passes once the world really matches it."""
    engine_handle, actions, _audit, _records = engine
    action_row = action("deployment.rollback_simulation")
    actions.add(action_row)

    get_sandbox().rollback("release-41", "unit test: perform the rollback for real")
    result = await engine_handle.verify_action(action_row.id, wait_seconds=0.0)

    assert result.outcome is VerificationOutcome.SUCCESS
    assert resolves_incident(result) is True


async def test_expectations_are_required_to_exist_before_execution(
    engine: tuple[Any, FakeActionRepository, FakeAuditRepository, FakeVerificationRepository],
) -> None:
    """With no declared target release the check cannot pass — it reports UNKNOWN."""
    engine_handle, actions, _audit, _records = engine
    action_row = action(
        "deployment.rollback_simulation",
        expected_state={"checks": [{"check": "release_active"}]},
    )
    actions.add(action_row)

    result = await engine_handle.verify_action(action_row.id, wait_seconds=0.0)

    assert result.outcome is VerificationOutcome.UNKNOWN
    assert "no target release was declared" in result.reason
    assert resolves_incident(result) is False


async def test_an_unreadable_source_is_unknown_and_never_a_pass(
    settings: Settings,
) -> None:
    """A dependency that cannot be reached means "we do not know", not "fine"."""

    class UnreadableIntegrations:
        async def deployment_state(self) -> dict[str, Any]:
            raise IntegrationUnavailable("deployment", "deployment system is unreachable")

    result = await run_check(
        "release_active",
        UnreadableIntegrations(),
        {"target_release": "release-41"},
        settings=settings,
    )

    assert result.outcome is VerificationOutcome.UNKNOWN
    assert "unavailable" in result.reason
    assert outcome_guidance(result.outcome).startswith("the evidence was insufficient")


async def test_too_few_samples_is_inconclusive_not_a_pass(
    settings: Settings, integrations: Any
) -> None:
    """A one-sample window cannot support a conclusion, so it must not pretend to."""
    result = await run_check(
        "metric_recovered",
        integrations,
        {"metric": "error_rate", "threshold": 0.02, "window_minutes": 1},
        settings=settings,
    )

    assert result.samples < MIN_SAMPLES_FOR_CONCLUSION
    assert result.outcome is VerificationOutcome.UNKNOWN
    assert "not enough to conclude" in result.reason


async def test_a_metric_that_did_not_recover_fails_the_check(
    settings: Settings, integrations: Any
) -> None:
    """The declared threshold is the pass mark; the observed value is reported either way."""
    result = await run_check(
        "metric_recovered",
        integrations,
        {"metric": "error_rate", "threshold": 0.02, "comparison": "below", "window_minutes": 30},
        settings=settings,
    )

    assert result.outcome is VerificationOutcome.FAILED
    assert result.observed["latest"] >= 0.02, "the raw numbers must be visible to a human"


# --------------------------------------------------------------------------- #
# Aggregation and consequences
# --------------------------------------------------------------------------- #


def _check(name: str, outcome: VerificationOutcome) -> CheckResult:
    return CheckResult(check=name, outcome=outcome, reason=f"{name} is {outcome.value.lower()}")


def test_aggregation_is_pessimistic() -> None:
    assert aggregate_outcomes([]) is VerificationOutcome.UNKNOWN
    assert (
        aggregate_outcomes(
            [_check("a", VerificationOutcome.SUCCESS), _check("b", VerificationOutcome.FAILED)]
        )
        is VerificationOutcome.FAILED
    ), "a contradiction outranks an inconclusive check"
    assert (
        aggregate_outcomes(
            [_check("a", VerificationOutcome.SUCCESS), _check("b", VerificationOutcome.UNKNOWN)]
        )
        is VerificationOutcome.UNKNOWN
    )
    assert (
        aggregate_outcomes(
            [_check("a", VerificationOutcome.SUCCESS), _check("b", VerificationOutcome.SUCCESS)]
        )
        is VerificationOutcome.SUCCESS
    )


def test_summary_never_hides_a_failure_behind_a_success_count() -> None:
    summary = summarise(
        [_check("a", VerificationOutcome.SUCCESS), _check("b", VerificationOutcome.FAILED)],
        VerificationOutcome.FAILED,
    )
    assert "1 passed, 1 failed" in summary
    assert "failures: b" in summary


def test_only_success_resolves_the_incident() -> None:
    for outcome in (VerificationOutcome.FAILED, VerificationOutcome.UNKNOWN):
        result = VerificationResult.model_validate(
            {
                "id": "VER-1",
                "incident_id": "INC-1",
                "outcome": outcome,
                "reason": "declared expectations were not met",
            }
        )
        assert resolves_incident(result) is False
    success = VerificationResult.model_validate(
        {
            "id": "VER-2",
            "incident_id": "INC-1",
            "outcome": VerificationOutcome.SUCCESS,
            "reason": "ok",
        }
    )
    assert resolves_incident(success) is True


async def test_every_run_is_audited_so_an_inconclusive_verdict_is_visible(
    engine: tuple[Any, FakeActionRepository, FakeAuditRepository, FakeVerificationRepository],
) -> None:
    """Started and completed are both recorded, with the checks and the outcome."""
    engine_handle, actions, audit, records = engine
    action_row = action(
        "deployment.rollback_simulation",
        expected_state={"checks": [{"check": "deployment_healthy"}]},
    )
    actions.add(action_row)

    await engine_handle.verify_action(action_row.id, wait_seconds=0.0)

    types = [event["event_type"] for event in audit.events]
    assert AuditEventType.VERIFICATION_STARTED in types
    assert AuditEventType.VERIFICATION_COMPLETED in types
    completed = next(
        event
        for event in audit.events
        if event["event_type"] is AuditEventType.VERIFICATION_COMPLETED
    )
    assert completed["outcome"] in {"SUCCESS", "FAILED", "UNKNOWN"}
    assert records.records[-1].duration_seconds >= 0.0


async def test_verification_of_an_unknown_action_is_an_error_not_a_pass(
    engine: tuple[Any, FakeActionRepository, FakeAuditRepository, FakeVerificationRepository],
) -> None:
    engine_handle, _actions, _audit, _records = engine
    with pytest.raises(ValueError):
        await engine_handle.verify_action("ACT-DOES-NOT-EXIST", wait_seconds=0.0)


def test_incident_level_verification_uses_explicit_checks(settings: Settings) -> None:
    """The default incident-level expectation is declared in code, not derived from results."""
    engine = VerificationEngine.__new__(VerificationEngine)
    engine.settings = settings
    checks = engine._plan_checks(None, {})
    assert checks == ["deployment_healthy", "metric_recovered"]
    assert engine._plan_checks("deployment.rollback_simulation", {}) == [
        "release_active",
        "deployment_healthy",
        "metric_recovered",
    ]
    assert engine._plan_checks("slack.notify", {}) == ["notification_delivered"]
    assert engine._plan_checks("jira.create_incident", {}) == ["issue_exists"]
    # An unknown tool is checked weakly and says so, rather than being skipped silently.
    assert engine._plan_checks("some.other.tool", {}) == ["tool_effect_recorded"]


async def test_a_missing_issue_is_a_failure_not_an_unknown(
    engine: tuple[Any, FakeActionRepository, FakeAuditRepository, FakeVerificationRepository],
) -> None:
    """A tracker without the ticket is a definitive "the escalation did not happen"."""
    engine_handle, actions, _audit, _records = engine
    action_row = action(
        "jira.create_incident",
        expected_state={
            "checks": [{"check": "issue_exists"}],
            "reference": "action.external_id",
        },
        result={"issue_key": "AIOPS-9999"},
    )
    actions.add(action_row)

    result = await engine_handle.verify_action(action_row.id, wait_seconds=0.0)

    assert result.outcome is VerificationOutcome.FAILED
    check = next(item for item in result.checks if item["check"] == "issue_exists")
    assert "does not exist" in check["reason"]


async def test_a_declared_reference_resolves_to_the_artefact_the_executor_created(
    engine: tuple[Any, FakeActionRepository, FakeAuditRepository, FakeVerificationRepository],
) -> None:
    """A Jira issue can only be verified after execution says *which* issue to look at.

    The expectation declared before execution is "an issue exists"; the key is execution data,
    resolved through a rule that was itself declared before execution.
    """
    engine_handle, actions, _audit, _records = engine
    action_row = action(
        "jira.create_incident",
        expected_state={
            "checks": [{"check": "issue_exists"}],
            "reference": "action.external_id",
            "description": "the escalation issue exists in the tracker",
        },
        result={"issue_key": "AIOPS-1042", "replayed": False, "simulated": True},
    )
    actions.add(action_row)

    provider = getattr(engine_handle.integrations, "jira", None)
    assert provider is not None
    await provider.create_issue(
        {"incident_id": "INC-VERIFY-1", "summary": "API error spike"}, "key-for-verification"
    )
    created = await provider.create_issue({"incident_id": "INC-VERIFY-1"}, "other-key")
    actions.actions[action_row.id] = action_row.model_copy(
        update={"result": {**action_row.result, "issue_key": created["issue_key"]}}
    )

    result = await engine_handle.verify_action(action_row.id, wait_seconds=0.0)

    assert result.outcome is VerificationOutcome.SUCCESS
    check = next(item for item in result.checks if item["check"] == "issue_exists")
    assert check["observed"]["key"] == created["issue_key"]


def test_run_state_machine_matches_the_verification_consequences() -> None:
    """RESOLVED is reachable only from VERIFYING — the state machine encodes the same rule."""
    from app.agent.state_machine import ALLOWED_TRANSITIONS

    assert AgentState.RESOLVED in ALLOWED_TRANSITIONS[AgentState.VERIFYING]
    assert AgentState.ESCALATED in ALLOWED_TRANSITIONS[AgentState.VERIFYING]
    assert Severity.SEV2.value == "SEV2"  # sanity: severity enum imported for the traceability gate
