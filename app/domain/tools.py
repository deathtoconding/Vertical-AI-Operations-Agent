"""Tool metadata and invocation records — the security boundary (OPS-051)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from app.domain.base import DomainModel, ORMBackedModel, ensure_utc, utcnow
from app.domain.enums import RiskLevel, ToolOutcome


class ToolSpec(DomainModel):
    """Everything policy needs to know about a capability, declared in one place."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
    description: str
    permission: str
    risk: RiskLevel
    requires_approval: bool = False
    timeout_seconds: float = 30.0
    max_retries: int = 2
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    idempotent: bool = True
    simulated: bool = False

    @property
    def is_high_risk(self) -> bool:
        return self.risk.at_least(RiskLevel.HIGH)


class ToolInvocation(ORMBackedModel):
    """Audit-grade record of one tool attempt, including denials and unknown tools."""

    id: str
    run_id: str | None = None
    incident_id: str | None = None
    tool_name: str
    risk: RiskLevel
    outcome: ToolOutcome
    duration_ms: float = 0.0
    request: dict[str, Any] = Field(default_factory=dict)
    response: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    actor: str = "system"
    simulated: bool = False
    occurred_at: datetime = Field(default_factory=utcnow)

    @field_validator("occurred_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class ToolResult(DomainModel):
    """What a tool reports back.

    ``success`` means *the call was made and answered*. It is deliberately **not** an
    assertion that the intended effect happened — that is verification's job (ADR-0004).
    """

    tool_name: str
    success: bool
    outcome: ToolOutcome
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    duration_ms: float = 0.0
    external_id: str | None = None
    replayed: bool = False
    simulated: bool = False


__all__ = ["ToolInvocation", "ToolResult", "ToolSpec"]
