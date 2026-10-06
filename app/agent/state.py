"""Run-state helpers (OPS-002).

The transition table lives in :mod:`app.agent.state_machine`; this module answers the questions
the rest of the application asks about a state — *is a human needed?*, *what stage is this?*,
*can it still advance?* — so that logic is not re-derived at call sites (and cannot drift).
"""

from __future__ import annotations

from app.agent.state_machine import ALLOWED_TRANSITIONS, assert_transition, is_allowed
from app.domain.enums import AgentState

#: Human-readable stage label for the API and the console.
STAGE_LABELS: dict[AgentState, str] = {
    AgentState.NEW: "run created",
    AgentState.DETECTED: "detection recorded",
    AgentState.INVESTIGATING: "gathering evidence",
    AgentState.PLANNED: "plan prepared",
    AgentState.WAITING_APPROVAL: "waiting for a human decision",
    AgentState.EXECUTING: "executing approved actions",
    AgentState.VERIFYING: "verifying the effect independently",
    AgentState.RESOLVED: "resolved and verified",
    AgentState.FAILED: "failed — a human must decide",
    AgentState.ESCALATED: "escalated to a human",
}


def describe(state: AgentState) -> str:
    """One-line description used in API responses and log context."""
    return STAGE_LABELS.get(state, state.value)


def requires_human(state: AgentState) -> bool:
    """True when the run cannot progress without a person.

    ``ESCALATED`` counts: an escalated run is not "finished", it is waiting for an operator,
    and the API must say so rather than reporting success.
    """
    return state is AgentState.WAITING_APPROVAL or state is AgentState.ESCALATED


def can_advance(state: AgentState) -> bool:
    return bool(ALLOWED_TRANSITIONS.get(state)) and not state.is_terminal


def next_states(state: AgentState) -> list[str]:
    return sorted(item.value for item in ALLOWED_TRANSITIONS.get(state, frozenset()))


__all__ = [
    "ALLOWED_TRANSITIONS",
    "STAGE_LABELS",
    "assert_transition",
    "can_advance",
    "describe",
    "is_allowed",
    "next_states",
    "requires_human",
]
