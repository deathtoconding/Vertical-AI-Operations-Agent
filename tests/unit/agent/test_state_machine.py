"""State machine tests (OPS-002).

These tests exist to prove one structural claim: **a plan containing an approval-gated action
cannot reach EXECUTING**. That property is not enforced by a caller remembering to check — it
is enforced by the transition table plus a mandatory guard.
"""

from __future__ import annotations

import pytest

from app.agent.state_machine import ALLOWED_TRANSITIONS, assert_transition, is_allowed
from app.core.errors import InvalidTransition
from app.domain.enums import AgentState

pytestmark = [pytest.mark.story("OPS-002"), pytest.mark.unit]


def test_every_state_appears_in_the_table() -> None:
    for state in AgentState:
        assert state in ALLOWED_TRANSITIONS


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (AgentState.NEW, AgentState.DETECTED),
        (AgentState.DETECTED, AgentState.INVESTIGATING),
        (AgentState.INVESTIGATING, AgentState.PLANNED),
        (AgentState.PLANNED, AgentState.WAITING_APPROVAL),
        (AgentState.WAITING_APPROVAL, AgentState.EXECUTING),
        (AgentState.EXECUTING, AgentState.VERIFYING),
        (AgentState.VERIFYING, AgentState.RESOLVED),
        (AgentState.VERIFYING, AgentState.ESCALATED),
        (AgentState.RESOLVED, AgentState.ESCALATED),
        (AgentState.FAILED, AgentState.INVESTIGATING),
        (AgentState.ESCALATED, AgentState.INVESTIGATING),
    ],
)
def test_documented_transitions_are_allowed(source: AgentState, target: AgentState) -> None:
    assert is_allowed(source, target)
    assert_transition(source, target, guard={"plan_has_approval_gate": False})


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (AgentState.NEW, AgentState.RESOLVED),
        (AgentState.NEW, AgentState.EXECUTING),
        (AgentState.DETECTED, AgentState.PLANNED),
        (AgentState.INVESTIGATING, AgentState.EXECUTING),
        (AgentState.PLANNED, AgentState.VERIFYING),
        (AgentState.EXECUTING, AgentState.RESOLVED),
        (AgentState.WAITING_APPROVAL, AgentState.RESOLVED),
        (AgentState.RESOLVED, AgentState.INVESTIGATING),
    ],
)
def test_shortcut_transitions_are_rejected(source: AgentState, target: AgentState) -> None:
    assert not is_allowed(source, target)
    with pytest.raises(InvalidTransition):
        assert_transition(source, target)


def test_planned_to_executing_requires_the_guard() -> None:
    with pytest.raises(InvalidTransition) as excinfo:
        assert_transition(AgentState.PLANNED, AgentState.EXECUTING)
    assert "guard" in str(excinfo.value)


def test_planned_to_executing_is_impossible_with_a_pending_approval() -> None:
    with pytest.raises(InvalidTransition) as excinfo:
        assert_transition(
            AgentState.PLANNED, AgentState.EXECUTING, guard={"plan_has_approval_gate": True}
        )
    assert "WAITING_APPROVAL" in str(excinfo.value)


def test_invalid_transition_is_counted() -> None:
    from app.core.telemetry import AGENT_TRANSITIONS, reset_for_tests

    reset_for_tests()
    with pytest.raises(InvalidTransition):
        assert_transition(AgentState.NEW, AgentState.RESOLVED)
    from app.core.telemetry import _normalise

    expected = _normalise(AGENT_TRANSITIONS._name)
    found = [
        metric.value
        for metric in AGENT_TRANSITIONS.collect()[0].samples
        if metric.labels.get("result") == "invalid" and _normalise(metric.name) == expected
    ]
    assert found and found[0] == 1.0, found


def test_no_state_can_reach_resolved_without_verifying() -> None:
    """Walk the graph: RESOLVED must only be reachable from VERIFYING."""
    predecessors = {
        state for state, targets in ALLOWED_TRANSITIONS.items() if AgentState.RESOLVED in targets
    }
    assert predecessors == {AgentState.VERIFYING}


def test_high_risk_states_have_no_direct_path_from_planning() -> None:
    """There is no PLANNED -> VERIFYING edge: execution and verification are always separate."""
    assert AgentState.VERIFYING not in ALLOWED_TRANSITIONS[AgentState.PLANNED]
    assert AgentState.EXECUTING in ALLOWED_TRANSITIONS[AgentState.WAITING_APPROVAL]
