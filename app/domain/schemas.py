"""HTTP request/response schemas (SEC-006).

Separating transport schemas from domain models is not ceremony — it is what keeps input
validation honest:

* every schema is ``extra="forbid"``, so a misspelled or unexpected field is a 422 instead of a
  silently ignored parameter (an attacker's favourite kind of bug);
* string lengths and list sizes are bounded *here*, at the edge, so no downstream layer has to
  defend itself against a 4 MB "reason" field;
* the domain objects stay free to evolve without changing the public contract by accident.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import ApprovalDecision, AutonomyLevel


class StrictModel(BaseModel):
    """Base for every API payload. Unknown fields are rejected, never ignored."""

    model_config = ConfigDict(extra="forbid")


class StartRunRequest(StrictModel):
    incident_id: str = Field(min_length=1, max_length=64, description="Incident to operate on.")
    autonomy: AutonomyLevel | None = Field(
        default=None, description="Override the configured autonomy level for this run."
    )


class ReasonRequest(StrictModel):
    reason: str = Field(default="", max_length=500)


class ApprovalDecisionRequest(StrictModel):
    decision: ApprovalDecision = Field(description="APPROVED or REJECTED.")
    payload_hash: str = Field(
        min_length=8,
        max_length=64,
        description="The payload hash shown to the approver; must match what is stored.",
    )
    reason: str = Field(default="", max_length=500)


class ScanRequest(StrictModel):
    service: str | None = Field(default=None, max_length=120)
    metrics: list[str] | None = Field(default=None, max_length=12)
    orchestrate: bool = Field(default=True, description="Run the agent on newly created incidents.")


class SimulateRequest(StrictModel):
    scenario: Literal["A", "B", "C", "normal"] = "A"
    orchestrate: bool = True
    reset: bool = True


class AutonomyRequest(StrictModel):
    level: AutonomyLevel
    reason: str = Field(default="", max_length=500)


class EvidenceQuery(StrictModel):
    limit: int = Field(default=200, ge=1, le=500)


__all__ = [
    "ApprovalDecisionRequest",
    "AutonomyRequest",
    "EvidenceQuery",
    "ReasonRequest",
    "ScanRequest",
    "SimulateRequest",
    "StartRunRequest",
    "StrictModel",
]
