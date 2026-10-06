"""Action planning from model output (OPS-060).

The planner is the first place model output is trusted with anything. These tests assume the
output is wrong: hallucinated tools, missing hints, parameters the tool never declared. The
requirement is that a bad proposal becomes a *recorded refusal*, never an exception and never
an execution.
"""

from __future__ import annotations

import pytest

from app.actions.planner import (
    RECOVERY_TARGETS,
    VERIFICATION_CHECKS,
    expected_state_for,
    plan_actions,
)
from app.core.config import Settings
from app.domain.actions import ActionRequest

pytestmark = [pytest.mark.story("OPS-060"), pytest.mark.unit]

KNOWN_TOOLS = frozenset({"deployment.rollback_simulation", "slack.notify", "jira.create_incident"})


def diagnosis(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "hypothesis": "release-42 introduced a regression in checkout error handling",
        "confidence": 0.62,
        "evidence_ids": ["EV-1", "EV-2"],
        "recommended_actions": [
            {
                "intent": "roll back the suspect release",
                "tool_hint": "deployment.rollback_simulation",
                "params": {"target_release": "release-41", "reason": "error spike"},
                "rationale": "error rate rose immediately after release-42",
            }
        ],
    }
    values.update(overrides)
    return values


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


def test_a_hallucinated_tool_is_refused_with_a_reason() -> None:
    plan = plan_actions(
        diagnosis(
            recommended_actions=[
                {"intent": "restart everything", "tool_hint": "kubectl.restart", "params": {}},
                {"intent": "read the database", "tool_hint": "sql.query_raw", "params": {}},
            ]
        ),
        incident_id="INC-1",
        known_tools=KNOWN_TOOLS,
    )

    assert plan.is_empty, "no unregistered capability may reach the executor"
    assert len(plan.rejected) == 2
    assert all("not registered" in item["reason"] for item in plan.rejected)
    assert {item["tool_name"] for item in plan.rejected} == {"kubectl.restart", "sql.query_raw"}


def test_a_proposal_without_a_tool_hint_is_refused_visibly() -> None:
    plan = plan_actions(
        diagnosis(recommended_actions=[{"intent": "fix it", "params": {"anything": True}}]),
        incident_id="INC-1",
        known_tools=KNOWN_TOOLS,
    )
    assert plan.is_empty
    assert plan.rejected[0]["reason"] == "no tool hint"
    assert plan.rejected[0]["intent"] == "fix it"


def test_a_plan_with_no_recommendations_is_empty_not_an_error() -> None:
    plan = plan_actions(diagnosis(recommended_actions=[]), incident_id="INC-1")
    assert plan.is_empty
    assert plan.rejected == []


# --------------------------------------------------------------------------- #
# Accepted proposals
# --------------------------------------------------------------------------- #


def test_registered_tools_become_ordered_requests_with_expected_state() -> None:
    plan = plan_actions(
        diagnosis(
            recommended_actions=[
                {
                    "intent": "notify",
                    "tool_hint": "slack.notify",
                    "params": {"channel": "#ops", "message": "investigating"},
                    "evidence_ids": ["EV-9"],
                },
                {
                    "intent": "roll back",
                    "tool_hint": "deployment.rollback_simulation",
                    "params": {"target_release": "release-41", "reason": "spike"},
                },
            ]
        ),
        incident_id="INC-1",
        metric="error_rate",
        known_tools=KNOWN_TOOLS,
    )

    assert [request.tool_name for request in plan.requests] == [
        "slack.notify",
        "deployment.rollback_simulation",
    ]
    assert [request.sequence for request in plan.requests] == [0, 1]
    assert all(request.incident_id == "INC-1" for request in plan.requests)

    notify, rollback = plan.requests
    assert notify.evidence_ids == ["EV-9"], "a proposal may cite its own evidence"
    assert rollback.evidence_ids == ["EV-1", "EV-2"], "otherwise the diagnosis evidence carries"
    assert rollback.expected_state.target_release == "release-41"
    assert rollback.expected_state.metric == "error_rate"
    assert rollback.expected_state.threshold == RECOVERY_TARGETS["error_rate"]


def test_expected_state_is_declared_before_execution_for_every_known_tool() -> None:
    for tool_name in VERIFICATION_CHECKS:
        state = expected_state_for(tool_name, {"target_release": "release-41"})
        assert state.checks, f"{tool_name} would leave verification nothing to check"
        assert state.description


def test_rollback_expectations_name_the_release_and_the_recovery_target(
    settings: Settings,
) -> None:
    state = expected_state_for(
        "deployment.rollback_simulation",
        {"target_release": "release-41"},
        metric="latency_p95",
        settings=settings,
    )
    assert state.target_release == "release-41"
    assert state.metric == "latency_p95"
    assert state.threshold == RECOVERY_TARGETS["latency_p95"]
    assert state.comparison == "below"
    assert state.window_seconds == settings.verification_window_seconds
    assert [check["check"] for check in state.checks] == [
        "release_active",
        "deployment_healthy",
        "metric_recovered",
    ]


def test_a_tool_without_a_post_condition_declares_nothing() -> None:
    """Verification must answer UNKNOWN, not invent a pass, for an unverifiable tool."""
    state = expected_state_for("github.read_commits", {"repository": "acme/checkout"})
    assert state.checks == []
    assert state.threshold is None


def test_planning_is_deterministic() -> None:
    first = plan_actions(diagnosis(), incident_id="INC-1", known_tools=KNOWN_TOOLS)
    second = plan_actions(diagnosis(), incident_id="INC-1", known_tools=KNOWN_TOOLS)
    assert [request.model_dump() for request in first.requests] == [
        request.model_dump() for request in second.requests
    ]


def test_requests_are_real_action_requests() -> None:
    plan = plan_actions(diagnosis(), incident_id="INC-1", known_tools=KNOWN_TOOLS)
    assert isinstance(plan.requests[0], ActionRequest)
    assert plan.rationale == diagnosis()["hypothesis"]
