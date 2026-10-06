"""Agent run and transition repositories (OPS-050)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.domain.agent_runs import AgentRun, RunTransition
from app.domain.enums import AgentState
from app.persistence.models.base import utcnow
from app.persistence.models.run import AgentRunRow, RunTransitionRow
from app.persistence.repositories.base import BaseRepository

NON_TERMINAL_STATES = (
    AgentState.NEW.value,
    AgentState.DETECTED.value,
    AgentState.INVESTIGATING.value,
    AgentState.PLANNED.value,
    AgentState.WAITING_APPROVAL.value,
    AgentState.EXECUTING.value,
    AgentState.VERIFYING.value,
)


class AgentRunRepository(BaseRepository[AgentRunRow, AgentRun]):
    row_class = AgentRunRow
    domain_class = AgentRun

    async def create(self, values: dict[str, Any]) -> AgentRun:
        row = AgentRunRow(**values)
        await self.add(row)
        await self.session.refresh(row)
        return self.to_domain(row)

    async def note(self, run_id: str, values: dict[str, Any]) -> AgentRun | None:
        """Persist stage data (diagnosis, plan, notes) **without** a state transition.

        A run that re-records ``INVESTIGATING -> INVESTIGATING`` would clutter the timeline and
        make the transition table meaningless; version is bumped only by real state changes.
        """
        row = await self.get_row(run_id)
        if row is None:
            return None
        self.apply(row, {**values, "updated_at": utcnow()})
        await self.session.flush()
        await self.session.refresh(row)
        return self.to_domain(row)

    async def get_for_incident(self, incident_id: str) -> AgentRun | None:
        result = await self.session.execute(
            select(AgentRunRow)
            .where(AgentRunRow.incident_id == incident_id)
            .order_by(AgentRunRow.started_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return self.to_domain(row) if row else None

    async def list_runs(
        self, *, state: AgentState | None = None, limit: int = 50
    ) -> list[AgentRun]:
        statement = select(AgentRunRow).order_by(AgentRunRow.started_at.desc()).limit(limit)
        if state is not None:
            statement = statement.where(AgentRunRow.state == state.value)
        result = await self.session.execute(statement)
        return self.to_domain_list(list(result.scalars().all()))

    async def list_interrupted(self, lease_seconds: float) -> list[AgentRun]:
        """Runs in a non-terminal state whose lease has expired (recovery candidates)."""
        cutoff = utcnow() - timedelta(seconds=lease_seconds)
        result = await self.session.execute(
            select(AgentRunRow)
            .where(
                AgentRunRow.state.in_(NON_TERMINAL_STATES),
                AgentRunRow.updated_at < cutoff,
            )
            .order_by(AgentRunRow.updated_at)
        )
        return self.to_domain_list(list(result.scalars().all()))

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
        """Apply a state change with optimistic concurrency.

        Returns ``(run, transition)``. Raises :class:`ConflictError` when another worker has
        already advanced the run, which is how two concurrent orchestrations of the same run
        are prevented from both executing an action.
        """
        from app.core.errors import ConflictError

        row = await self.get_row(run_id)
        if row is None:
            from app.core.errors import NotFoundError

            raise NotFoundError(f"Agent run {run_id} does not exist.")

        if expected_version is not None and row.version != expected_version:
            raise ConflictError(
                "The run was modified by another worker.",
                details={"expected_version": expected_version, "actual_version": row.version},
            )

        from_state = AgentState(row.state)
        now = utcnow()
        row.state = to_state.value
        row.version = row.version + 1
        row.updated_at = now
        if to_state.is_terminal or to_state is AgentState.FAILED:
            row.finished_at = now
        if extra:
            self.apply(row, extra)

        transition = RunTransitionRow(
            run_id=run_id,
            incident_id=row.incident_id,
            from_state=from_state.value,
            to_state=to_state.value,
            actor=actor,
            reason=reason,
            version=row.version,
            occurred_at=now,
        )
        self.session.add(transition)
        await self.session.flush()
        await self.session.refresh(row)
        await self.session.refresh(transition)
        return self.to_domain(row), RunTransition.model_validate(transition, from_attributes=True)

    async def list_transitions(self, run_id: str) -> list[RunTransition]:
        result = await self.session.execute(
            select(RunTransitionRow)
            .where(RunTransitionRow.run_id == run_id)
            .order_by(RunTransitionRow.occurred_at, RunTransitionRow.version)
        )
        return [
            RunTransition.model_validate(row, from_attributes=True)
            for row in result.scalars().all()
        ]

    async def claim(self, run_id: str, lease_seconds: float) -> AgentRun | None:
        """Extend the run lease. Returns ``None`` when the run is already leased elsewhere."""
        row = await self.get_row(run_id)
        if row is None:
            return None
        now = utcnow()
        if row.lease_expires_at is not None and row.lease_expires_at > now:
            holder_updated = row.updated_at
            if holder_updated > now - timedelta(seconds=lease_seconds):
                return None
        row.lease_expires_at = now + timedelta(seconds=lease_seconds)
        await self.session.flush()
        await self.session.refresh(row)
        return self.to_domain(row)

    async def last_transition_at(self, run_id: str) -> datetime | None:
        result = await self.session.execute(
            select(RunTransitionRow.occurred_at)
            .where(RunTransitionRow.run_id == run_id)
            .order_by(RunTransitionRow.occurred_at.desc())
            .limit(1)
        )
        value = result.scalar_one_or_none()
        return value if isinstance(value, datetime) else None


__all__ = ["NON_TERMINAL_STATES", "AgentRunRepository"]
