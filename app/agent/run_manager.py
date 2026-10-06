"""Run lifecycle management (OPS-050).

Everything that mutates an ``agent_runs`` row goes through this class, so three properties are
enforced in one place rather than remembered in seven:

1. **Optimistic concurrency** — a transition carries the version it expects; a second worker
   loses with a :class:`ConflictError` instead of silently double-advancing a run.
2. **Leases** — a long job claims a lease so a scheduled recovery pass does not fight it.
3. **Stage notes are not transitions** — recording a diagnosis is not a state change, and
   pretending it is would fill the timeline with ``INVESTIGATING -> INVESTIGATING`` noise.
"""

from __future__ import annotations

from typing import Any

from app.domain.agent_runs import AgentRun
from app.domain.enums import AgentState
from app.persistence.models.base import new_prefixed_id
from app.persistence.repositories.agent_runs import AgentRunRepository


class RunManager:
    """Thin, testable wrapper over :class:`AgentRunRepository`."""

    def __init__(self, runs: AgentRunRepository) -> None:
        self.runs = runs

    async def start(
        self,
        incident_id: str,
        *,
        actor: str,
        autonomy_level: str,
        simulated: bool = False,
    ) -> AgentRun:
        return await self.runs.create(
            {
                "id": new_prefixed_id("RUN"),
                "incident_id": incident_id,
                "state": AgentState.NEW.value,
                "stage": "created",
                "actor": actor,
                "autonomy_level": autonomy_level,
                "simulated": simulated,
            }
        )

    async def get_or_start(
        self,
        incident_id: str,
        *,
        actor: str,
        autonomy_level: str,
        simulated: bool = False,
    ) -> tuple[AgentRun, bool]:
        """Return ``(run, created)``. Idempotent: one run per incident."""
        existing = await self.runs.get_for_incident(incident_id)
        if existing is not None:
            return existing, False
        return (
            await self.start(
                incident_id, actor=actor, autonomy_level=autonomy_level, simulated=simulated
            ),
            True,
        )

    async def advance(
        self,
        run: AgentRun,
        to_state: AgentState,
        *,
        actor: str,
        reason: str,
        extra: dict[str, Any] | None = None,
    ) -> AgentRun:
        updated, _ = await self.runs.transition(
            run.id,
            to_state,
            actor=actor,
            reason=reason,
            expected_version=run.version,
            extra=extra,
        )
        return updated

    async def note(self, run: AgentRun, values: dict[str, Any]) -> AgentRun:
        updated = await self.runs.note(run.id, values)
        return updated or run

    async def claim(self, run_id: str, lease_seconds: float) -> AgentRun | None:
        return await self.runs.claim(run_id, lease_seconds)

    async def transitions(self, run_id: str) -> list[Any]:
        return await self.runs.list_transitions(run_id)


__all__ = ["RunManager"]
