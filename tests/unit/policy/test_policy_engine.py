"""Policy engine tests (SEC-002, OPS-051).

The policy engine is the authorization boundary, so these tests are written as *attacks*:
each one asks "can something that should not happen happen anyway?".
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.core.config import Settings
from app.core.security import Actor
from app.domain.enums import (
    AutonomyLevel,
    IncidentStatus,
    IncidentType,
    PolicyDecisionType,
    RiskLevel,
    Role,
    Severity,
)
from app.domain.incidents import Incident
from app.policy.engine import PolicyEngine
from app.tools.registry import build_default_registry

pytestmark = [pytest.mark.story("OPS-051"), pytest.mark.security, pytest.mark.unit]


@pytest.fixture
def engine(settings: Settings) -> PolicyEngine:
    return PolicyEngine(build_default_registry(), settings)


@pytest.fixture
def incident(fixed_now: dt.datetime) -> Incident:
    return Incident(
        id="INC-TEST",
        incident_type=IncidentType.API_ERROR_SPIKE,
        severity=Severity.SEV2,
        status=IncidentStatus.OPEN,
        title="error spike",
        service="checkout-service",
        metric="error_rate",
        dedup_key="dedup-test",
        detected_at=fixed_now,
    )


def test_viewer_cannot_call_any_write_tool(engine: PolicyEngine) -> None:
    viewer = Actor(actor_id="viewer", role=Role.VIEWER)
    for tool in ("jira.create_incident", "slack.notify", "deployment.rollback_simulation"):
        decision = engine.evaluate(tool, actor=viewer)
        assert decision.decision is PolicyDecisionType.DENY
        assert decision.risk is not RiskLevel.CRITICAL or decision.permission == "none"


def test_operator_may_not_roll_back(engine: PolicyEngine) -> None:
    operator = Actor(actor_id="operator", role=Role.OPERATOR)
    decision = engine.evaluate(
        "deployment.rollback_simulation",
        actor=operator,
        params={"target_release": "release-41", "reason": "rollback"},
    )
    assert decision.decision is PolicyDecisionType.DENY


def test_sre_rollback_requires_approval_never_allow(engine: PolicyEngine) -> None:
    sre = Actor(actor_id="sre", role=Role.SRE)
    decision = engine.evaluate(
        "deployment.rollback_simulation",
        actor=sre,
        params={"target_release": "release-41", "reason": "regression"},
    )
    assert decision.decision is PolicyDecisionType.REQUIRE_APPROVAL
    assert decision.requires_approval is True
    assert "high_risk" in decision.reason


def test_admin_rollback_also_requires_approval(engine: PolicyEngine) -> None:
    """Rank must not buy a bypass: the highest role still cannot roll back unilaterally."""
    admin = Actor(actor_id="admin", role=Role.ADMIN)
    decision = engine.evaluate(
        "deployment.rollback_simulation",
        actor=admin,
        params={"target_release": "release-41", "reason": "regression"},
    )
    assert decision.decision is PolicyDecisionType.REQUIRE_APPROVAL


def test_unknown_tool_is_denied_by_default(engine: PolicyEngine) -> None:
    sre = Actor(actor_id="sre", role=Role.SRE)
    decision = engine.evaluate("shell.exec", actor=sre, params={"command": "rm -rf /"})
    assert decision.decision is PolicyDecisionType.DENY
    assert decision.risk is RiskLevel.CRITICAL
    assert "not registered" in decision.reason


def test_observe_only_denies_everything_including_low_risk(
    engine: PolicyEngine, settings: Settings
) -> None:
    sre = Actor(actor_id="sre", role=Role.SRE)
    decision = engine.evaluate(
        "slack.notify", actor=sre, params={"text": "hi"}, autonomy=AutonomyLevel.OBSERVE_ONLY
    )
    assert decision.decision is PolicyDecisionType.DENY
    assert "observe_only" in decision.reason


def test_cross_incident_action_is_denied(engine: PolicyEngine, incident: Incident) -> None:
    """A confused or injected plan may not act on a different incident."""
    sre = Actor(actor_id="sre", role=Role.SRE)
    decision = engine.evaluate(
        "jira.create_incident",
        actor=sre,
        incident=incident,
        params={"incident_id": "INC-OTHER", "summary": "x"},
    )
    assert decision.decision is PolicyDecisionType.DENY
    assert "cross_incident_action" in decision.reason


def test_terminal_incident_accepts_no_new_actions(engine: PolicyEngine, incident: Incident) -> None:
    resolved = incident.model_copy(update={"status": IncidentStatus.RESOLVED})
    sre = Actor(actor_id="sre", role=Role.SRE)
    decision = engine.evaluate("slack.notify", actor=sre, incident=resolved, params={"text": "hi"})
    assert decision.decision is PolicyDecisionType.DENY
    assert "terminal" in decision.reason


def test_disabled_tool_is_denied_even_for_admin(engine: PolicyEngine) -> None:
    admin = Actor(actor_id="admin", role=Role.ADMIN)
    decision = engine.evaluate(
        "slack.notify",
        actor=admin,
        params={"text": "hi"},
        disabled_tools=frozenset({"slack.notify"}),
    )
    assert decision.decision is PolicyDecisionType.DENY
    assert "disabled" in decision.reason


def test_rollback_loop_guard_denies_after_the_configured_limit(
    engine: PolicyEngine, settings: Settings
) -> None:
    sre = Actor(actor_id="sre", role=Role.SRE)
    decision = engine.evaluate(
        "deployment.rollback_simulation",
        actor=sre,
        params={"target_release": "release-41", "reason": "again"},
        prior_rollbacks=settings.max_rollbacks_per_hour,
    )
    assert decision.decision is PolicyDecisionType.DENY
    assert "rollback_loop_guard" in decision.reason


def test_repeat_rollbacks_escalate_risk_level(engine: PolicyEngine, incident: Incident) -> None:
    """Context can raise risk: a repeated rollback of the same incident is CRITICAL.

    A second rollback means the first diagnosis was wrong; treating it as "just another
    high-risk action" is how a loop gets started.
    """
    spec = build_default_registry().get("deployment.rollback_simulation").spec
    first = engine.assess_risk(
        spec, incident=incident, params={"target_release": "release-41"}, prior_rollbacks=0
    )
    repeat = engine.assess_risk(
        spec, incident=incident, params={"target_release": "release-41"}, prior_rollbacks=1
    )
    assert repeat.effective_risk is RiskLevel.CRITICAL
    assert repeat.effective_risk.rank > first.effective_risk.rank
    assert "repeat_high_risk_action" in repeat.escalators


def test_sev1_escalates_medium_actions_to_high(engine: PolicyEngine, incident: Incident) -> None:
    sev1 = incident.model_copy(update={"severity": Severity.SEV1})
    spec = build_default_registry().get("slack.notify").spec
    assessment = engine.assess_risk(spec, incident=sev1)
    assert assessment.effective_risk is RiskLevel.HIGH
    assert "severity_sev1" in assessment.escalators


def test_destructive_parameters_escalate_risk(engine: PolicyEngine) -> None:
    spec = build_default_registry().get("slack.notify").spec
    assessment = engine.assess_risk(spec, params={"skip_verification": True, "text": "x"})
    assert assessment.effective_risk.at_least(RiskLevel.HIGH)
    assert any(item.startswith("destructive_parameter:") for item in assessment.escalators)


def test_policy_module_never_imports_the_llm() -> None:
    """A model cannot argue with code that never reads model output (ADR-0002)."""
    import pathlib

    for path in pathlib.Path("app/policy").glob("*.py"):
        source = path.read_text()
        assert "app.llm" not in source, f"{path} imports the LLM layer"
        assert "app.investigation" not in source, f"{path} imports the investigation layer"
