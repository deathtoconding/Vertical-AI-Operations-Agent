"""ORM models. Importing this package registers every table on ``Base.metadata``."""

from app.persistence.models.action import ActionRow, ApprovalRow, VerificationRow
from app.persistence.models.audit import AuditEventRow, IdempotencyKeyRow
from app.persistence.models.evidence import EvidenceRow
from app.persistence.models.incident import AnomalyRow, IncidentRow
from app.persistence.models.run import AgentRunRow, RunTransitionRow, ToolInvocationRow

ALL_MODELS = (
    IncidentRow,
    AnomalyRow,
    EvidenceRow,
    AgentRunRow,
    RunTransitionRow,
    ToolInvocationRow,
    ActionRow,
    ApprovalRow,
    VerificationRow,
    AuditEventRow,
    IdempotencyKeyRow,
)

__all__ = [
    "ALL_MODELS",
    "ActionRow",
    "AgentRunRow",
    "AnomalyRow",
    "ApprovalRow",
    "AuditEventRow",
    "EvidenceRow",
    "IdempotencyKeyRow",
    "IncidentRow",
    "RunTransitionRow",
    "ToolInvocationRow",
    "VerificationRow",
]
