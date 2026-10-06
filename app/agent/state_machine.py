"""Agent run state machine (OPS-002).

The transition table lives here as executable data rather than as prose in a design document,
because the guarantees the system makes depend on it:

* ``PLANNED -> EXECUTING`` is only legal when **every** planned action is permitted without
  approval. A plan containing a high-risk action has no edge to ``EXECUTING`` at all, so
  "the model talked its way past approval" is not a bug that can happen — the state simply
  cannot reach execution without passing through ``WAITING_APPROVAL``.
* An invalid transition raises :class:`~app.core.errors.InvalidTransition`, increments
  ``aiops_agent_transitions_total{result="invalid"}`` and writes an audit event. A rejected
  transition is evidence, not a log line nobody reads.
"""

from __future__ import annotations

from typing import Any, Final

from app.core.errors import InvalidTransition
from app.core.logging import get_logger
from app.core.telemetry import AGENT_TRANSITIONS
from app.domain.enums import AgentState, AuditEventType

logger = get_logger(__name__)

#: The documented transition table (``docs/architecture/agent-runtime.md`` §2).
ALLOWED_TRANSITIONS: Final[dict[AgentState, frozenset[AgentState]]] = {
    AgentState.NEW: frozenset({AgentState.DETECTED, AgentState.FAILED, AgentState.ESCALATED}),
    AgentState.DETECTED: frozenset(
        {AgentState.INVESTIGATING, AgentState.FAILED, AgentState.ESCALATED}
    ),
    AgentState.INVESTIGATING: frozenset(
        {AgentState.PLANNED, AgentState.FAILED, AgentState.ESCALATED}
    ),
    AgentState.PLANNED: frozenset(
        {AgentState.WAITING_APPROVAL, AgentState.EXECUTING, AgentState.FAILED, AgentState.ESCALATED}
    ),
    AgentState.WAITING_APPROVAL: frozenset(
        {AgentState.EXECUTING, AgentState.PLANNED, AgentState.FAILED, AgentState.ESCALATED}
    ),
    AgentState.EXECUTING: frozenset(
        {AgentState.VERIFYING, AgentState.FAILED, AgentState.ESCALATED}
    ),
    AgentState.VERIFYING: frozenset({AgentState.RESOLVED, AgentState.FAILED, AgentState.ESCALATED}),
    AgentState.RESOLVED: frozenset({AgentState.ESCALATED}),
    AgentState.FAILED: frozenset({AgentState.INVESTIGATING, AgentState.ESCALATED}),
    AgentState.ESCALATED: frozenset({AgentState.INVESTIGATING}),
}


def is_allowed(from_state: AgentState, to_state: AgentState) -> bool:
    return to_state in ALLOWED_TRANSITIONS.get(from_state, frozenset())


def assert_transition(
    from_state: AgentState,
    to_state: AgentState,
    *,
    guard: dict[str, Any] | None = None,
) -> None:
    """Raise unless the transition is legal under the table *and* the guard passes.

    Args:
        guard: contextual facts the transition needs. ``{"plan_has_approval_gate": bool}``
            is required for ``PLANNED -> EXECUTING``; the guard is deliberately not
            optional for that edge.
    """
    if not is_allowed(from_state, to_state):
        AGENT_TRANSITIONS.labels(
            from_state=from_state.value, to_state=to_state.value, result="invalid"
        ).inc()
        raise InvalidTransition(
            f"Transition {from_state.value} -> {to_state.value} is not permitted.",
            details={"from_state": from_state.value, "to_state": to_state.value},
        )

    if from_state is AgentState.PLANNED and to_state is AgentState.EXECUTING:
        facts = guard or {}
        if "plan_has_approval_gate" not in facts:
            AGENT_TRANSITIONS.labels(
                from_state=from_state.value, to_state=to_state.value, result="invalid"
            ).inc()
            raise InvalidTransition(
                "PLANNED -> EXECUTING requires the approval guard to be evaluated.",
                details={"missing_guard": "plan_has_approval_gate"},
            )
        if facts["plan_has_approval_gate"]:
            AGENT_TRANSITIONS.labels(
                from_state=from_state.value, to_state=to_state.value, result="invalid"
            ).inc()
            raise InvalidTransition(
                "This plan contains actions that require human approval; the run must pass "
                "through WAITING_APPROVAL.",
                details={"reason": "plan_has_approval_gate"},
            )

    AGENT_TRANSITIONS.labels(
        from_state=from_state.value, to_state=to_state.value, result="ok"
    ).inc()


async def record_transition_audit(
    audit: Any,
    *,
    from_state: AgentState,
    to_state: AgentState,
    run_id: str,
    incident_id: str,
    actor: str,
    role: str,
    reason: str = "",
    outcome: str = "ok",
) -> None:
    """Every transition — legal or rejected — is appended to the audit trail."""
    await audit.append(
        AuditEventType.RUN_STATE_CHANGED,
        actor=actor,
        role=role,
        incident_id=incident_id,
        agent_run_id=run_id,
        outcome=outcome,
        reason=reason,
        payload={"from_state": from_state.value, "to_state": to_state.value},
    )


__all__ = ["ALLOWED_TRANSITIONS", "assert_transition", "is_allowed", "record_transition_audit"]
