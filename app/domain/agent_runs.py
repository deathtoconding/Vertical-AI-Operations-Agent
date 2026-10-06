"""Agent run domain objects (OPS-002, OPS-050)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pydantic import Field, field_validator

from app.domain.base import ORMBackedModel, ensure_utc, utcnow
from app.domain.enums import AgentState, AutonomyLevel


class AgentRun(ORMBackedModel):
    """One execution of the agent workflow for one incident, with durable state."""

    id: str
    incident_id: str
    state: AgentState
    version: int = 1
    stage: str | None = None
    actor: str = "system"
    autonomy_level: AutonomyLevel = AutonomyLevel.APPROVAL_REQUIRED
    reasoner: str | None = None
    degraded_reason: str | None = None
    diagnosis: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    failure_reason: str | None = None
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    lease_expires_at: datetime | None = None
    notes: list[str] = Field(default_factory=list)
    simulated: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @field_validator("started_at", "created_at", "updated_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @property
    def is_terminal(self) -> bool:
        return self.state.is_terminal

    def lease(self, seconds: float) -> datetime:
        return utcnow() + timedelta(seconds=seconds)


class RunTransition(ORMBackedModel):
    """An append-only record of one state change."""

    id: str
    run_id: str
    incident_id: str
    from_state: AgentState
    to_state: AgentState
    actor: str
    reason: str = ""
    version: int
    occurred_at: datetime = Field(default_factory=utcnow)

    @field_validator("occurred_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class RunSnapshot(ORMBackedModel):
    """The orchestrator's answer for one ``step()`` call — what the API returns."""

    run: AgentRun
    transitions: list[RunTransition] = Field(default_factory=list)
    actions: list[dict[str, Any]] = Field(default_factory=list)
    verification: dict[str, Any] | None = None
    requires_human: bool = False
    pending_approval_id: str | None = None
    next_action: str | None = None


__all__ = ["AgentRun", "RunSnapshot", "RunTransition"]
