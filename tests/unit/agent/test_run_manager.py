"""Run lifecycle management (OPS-050).

Every mutation of an ``agent_runs`` row goes through :class:`RunManager`, so the guarantees it
must hold are tested here as *behaviour under conflict*: a second worker, a stale version, a
lease held elsewhere, and a stage note that must not look like a state transition.

The repository is replaced by a faithful in-memory double: the point of this file is the
manager's logic, and the real repository's SQL behaviour has its own tests against real
PostgreSQL in ``tests/integration/database/test_persistence.py``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from app.agent.run_manager import RunManager
from app.core.errors import ConflictError, NotFoundError
from app.domain.agent_runs import AgentRun, RunTransition
from app.domain.enums import AgentState, AutonomyLevel
from app.persistence.repositories.agent_runs import NON_TERMINAL_STATES

pytestmark = [pytest.mark.story("OPS-050"), pytest.mark.unit]

NOW = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.UTC)


class FakeRunRepository:
    """In-memory mirror of :class:`AgentRunRepository` for the manager's contract."""

    def __init__(self) -> None:
        self.runs: dict[str, AgentRun] = {}
        self.transitions: list[RunTransition] = []
        self._counter = 0
        self.lease_holders: dict[str, dt.datetime] = {}

    async def create(self, values: dict[str, Any]) -> AgentRun:
        run = AgentRun.model_validate(
            {
                "state": AgentState.NEW,
                "version": 1,
                "started_at": NOW,
                "created_at": NOW,
                "updated_at": NOW,
                **values,
            }
        )
        self.runs[run.id] = run
        self._counter += 1
        return run

    async def get_for_incident(self, incident_id: str) -> AgentRun | None:
        matches = [run for run in self.runs.values() if run.incident_id == incident_id]
        return max(matches, key=lambda run: run.started_at) if matches else None

    async def transition(
        self,
        run_id: str,
        to_state: AgentState,
        *,
        actor: str,
        reason: str = "",
        expected_version: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> tuple[AgentRun, RunTransition]:
        run = self.runs.get(run_id)
        if run is None:
            raise NotFoundError(f"Agent run {run_id} does not exist.")
        if expected_version is not None and run.version != expected_version:
            raise ConflictError(
                "The run was modified by another worker.",
                details={"expected_version": expected_version, "actual_version": run.version},
            )
        from_state = run.state
        # Mirrors the real repository: a terminal state *or* FAILED closes the run (FAILED is
        # not a terminal state — recovery may still resume it — but it is finished for now).
        closes = to_state.is_terminal or to_state is AgentState.FAILED
        updated = run.model_copy(
            update={
                "state": to_state,
                "version": run.version + 1,
                "updated_at": NOW,
                "finished_at": NOW if closes else run.finished_at,
                **(extra or {}),
            }
        )
        self.runs[run_id] = updated
        transition = RunTransition(
            id=f"TRN-{len(self.transitions) + 1}",
            run_id=run_id,
            incident_id=run.incident_id,
            from_state=from_state,
            to_state=to_state,
            actor=actor,
            reason=reason,
            version=updated.version,
            occurred_at=NOW,
        )
        self.transitions.append(transition)
        return updated, transition

    async def list_transitions(self, run_id: str) -> list[RunTransition]:
        return [item for item in self.transitions if item.run_id == run_id]

    async def note(self, run_id: str, values: dict[str, Any]) -> AgentRun | None:
        run = self.runs.get(run_id)
        if run is None:
            return None
        updated = run.model_copy(update={**values, "updated_at": NOW})
        self.runs[run_id] = updated
        return updated

    async def claim(self, run_id: str, lease_seconds: float) -> AgentRun | None:
        """Mirrors the real rule: a live lease held by *another* worker is not taken over."""
        run = self.runs.get(run_id)
        if run is None:
            return None
        holder = self.lease_holders.get(run_id)
        # A competing worker claimed it, and the request's lease would not outlive that claim.
        if (
            holder is not None
            and holder > NOW
            and holder - NOW > dt.timedelta(seconds=lease_seconds)
        ):
            return None
        deadline = NOW + dt.timedelta(seconds=lease_seconds)
        self.lease_holders[run_id] = deadline
        updated = run.model_copy(update={"lease_expires_at": deadline})
        self.runs[run_id] = updated
        return updated


@pytest.fixture
def repository() -> FakeRunRepository:
    return FakeRunRepository()


@pytest.fixture
def manager(repository: FakeRunRepository) -> RunManager:
    return RunManager(repository)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Creating and resuming
# --------------------------------------------------------------------------- #


async def test_start_creates_a_run_in_the_initial_state(manager: RunManager) -> None:
    run = await manager.start(
        "INC-1", actor="sre-1", autonomy_level=AutonomyLevel.APPROVAL_REQUIRED.value
    )
    assert run.incident_id == "INC-1"
    assert run.state is AgentState.NEW
    assert run.version == 1
    assert run.actor == "sre-1"
    assert run.id.startswith("RUN-")


async def test_get_or_start_is_idempotent_for_one_incident(
    manager: RunManager, repository: FakeRunRepository
) -> None:
    first, created_first = await manager.get_or_start(
        "INC-1", actor="sre-1", autonomy_level=AutonomyLevel.APPROVAL_REQUIRED.value
    )
    second, created_second = await manager.get_or_start(
        "INC-1", actor="system", autonomy_level=AutonomyLevel.OBSERVE_ONLY.value
    )

    assert created_first is True
    assert created_second is False
    assert first.id == second.id, "a second run would double-execute the same incident"
    assert len(repository.runs) == 1


async def test_different_incidents_get_different_runs(manager: RunManager) -> None:
    first, _ = await manager.get_or_start(
        "INC-1", actor="sre-1", autonomy_level=AutonomyLevel.APPROVAL_REQUIRED.value
    )
    second, _ = await manager.get_or_start(
        "INC-2", actor="sre-1", autonomy_level=AutonomyLevel.APPROVAL_REQUIRED.value
    )
    assert first.id != second.id


# --------------------------------------------------------------------------- #
# Transitions
# --------------------------------------------------------------------------- #


async def test_advance_bumps_the_version_and_records_a_transition(manager: RunManager) -> None:
    run = await manager.start(
        "INC-1", actor="sre-1", autonomy_level=AutonomyLevel.APPROVAL_REQUIRED.value
    )

    updated = await manager.advance(
        run, AgentState.DETECTED, actor="sre-1", reason="detection persisted"
    )

    assert updated.state is AgentState.DETECTED
    assert updated.version == 2
    transitions = await manager.transitions(run.id)
    assert len(transitions) == 1
    assert (transitions[0].from_state, transitions[0].to_state) == (
        AgentState.NEW,
        AgentState.DETECTED,
    )
    assert transitions[0].actor == "sre-1"
    assert transitions[0].reason == "detection persisted"


async def test_a_stale_version_loses_instead_of_double_advancing(manager: RunManager) -> None:
    """Two workers orchestrating the same run must not both execute an action."""
    run = await manager.start(
        "INC-1", actor="sre-1", autonomy_level=AutonomyLevel.APPROVAL_REQUIRED.value
    )
    fresh = await manager.advance(run, AgentState.DETECTED, actor="worker-a", reason="first")

    with pytest.raises(ConflictError) as error:
        await manager.advance(run, AgentState.PLANNED, actor="worker-b", reason="second")

    assert error.value.details == {"expected_version": 1, "actual_version": 2}
    assert fresh.version == 2


async def test_extra_state_is_persisted_with_the_transition(manager: RunManager) -> None:
    run = await manager.start(
        "INC-1", actor="sre-1", autonomy_level=AutonomyLevel.APPROVAL_REQUIRED.value
    )
    updated = await manager.advance(
        run,
        AgentState.FAILED,
        actor="sre-1",
        reason="investigation crashed",
        extra={"failure_reason": "reasoner unavailable"},
    )
    assert updated.failure_reason == "reasoner unavailable"
    assert updated.state is AgentState.FAILED
    assert updated.finished_at is not None, "a terminal run must be closed"


async def test_transitions_are_returned_in_order(manager: RunManager) -> None:
    run = await manager.start(
        "INC-1", actor="sre-1", autonomy_level=AutonomyLevel.APPROVAL_REQUIRED.value
    )
    for state in (AgentState.DETECTED, AgentState.INVESTIGATING, AgentState.PLANNED):
        run = await manager.advance(run, state, actor="sre-1", reason=f"move to {state.value}")

    transitions = await manager.transitions(run.id)
    assert [item.version for item in transitions] == [2, 3, 4]
    assert [item.to_state for item in transitions] == [
        AgentState.DETECTED,
        AgentState.INVESTIGATING,
        AgentState.PLANNED,
    ]


# --------------------------------------------------------------------------- #
# Notes
# --------------------------------------------------------------------------- #


async def test_note_records_stage_data_without_a_state_change(manager: RunManager) -> None:
    run = await manager.start(
        "INC-1", actor="sre-1", autonomy_level=AutonomyLevel.APPROVAL_REQUIRED.value
    )
    run = await manager.advance(run, AgentState.INVESTIGATING, actor="sre-1", reason="start")

    updated = await manager.note(
        run, {"stage": "evidence_collected", "diagnosis": {"hypothesis": "release-42"}}
    )

    assert updated.stage == "evidence_collected"
    assert updated.diagnosis == {"hypothesis": "release-42"}
    assert updated.state is AgentState.INVESTIGATING
    assert updated.version == run.version, "a note is not a transition"
    assert await manager.transitions(run.id) == await manager.transitions(run.id)
    assert len(await manager.transitions(run.id)) == 1


async def test_note_on_a_missing_run_returns_the_run_it_was_given(manager: RunManager) -> None:
    orphan = AgentRun(
        id="RUN-MISSING",
        incident_id="INC-1",
        state=AgentState.NEW,
        stage="created",
    )
    result = await manager.note(orphan, {"stage": "lost"})
    assert result.id == "RUN-MISSING", "a lost write must not become an AttributeError later"


# --------------------------------------------------------------------------- #
# Leases
# --------------------------------------------------------------------------- #


async def test_claim_takes_a_free_lease(manager: RunManager) -> None:
    run = await manager.start(
        "INC-1", actor="sre-1", autonomy_level=AutonomyLevel.APPROVAL_REQUIRED.value
    )
    claimed = await manager.claim(run.id, 120)
    assert claimed is not None
    assert claimed.lease_expires_at is not None


async def test_claim_refuses_a_lease_held_by_another_worker(
    manager: RunManager, repository: FakeRunRepository
) -> None:
    run = await manager.start(
        "INC-1", actor="sre-1", autonomy_level=AutonomyLevel.APPROVAL_REQUIRED.value
    )
    repository.lease_holders[run.id] = NOW + dt.timedelta(seconds=600)

    assert await manager.claim(run.id, 120) is None


async def test_claim_on_a_missing_run_is_none(manager: RunManager) -> None:
    assert await manager.claim("RUN-NOPE", 60) is None


# --------------------------------------------------------------------------- #
# Contract with the repository
# --------------------------------------------------------------------------- #


def test_interrupted_states_are_the_non_terminal_ones() -> None:
    """Recovery scans exactly these states; a missing one means a stuck run nobody notices."""
    assert set(NON_TERMINAL_STATES) == {
        AgentState.NEW.value,
        AgentState.DETECTED.value,
        AgentState.INVESTIGATING.value,
        AgentState.PLANNED.value,
        AgentState.WAITING_APPROVAL.value,
        AgentState.EXECUTING.value,
        AgentState.VERIFYING.value,
    }
