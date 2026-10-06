"""Action, approval, verification and tool-invocation repositories (EPIC-07)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from app.domain.actions import Action, Approval, action_payload, canonical_hash
from app.domain.enums import ActionStatus, ApprovalDecision, RiskLevel, ToolOutcome
from app.domain.tools import ToolInvocation
from app.domain.verification import VerificationResult
from app.persistence.models.action import ActionRow, ApprovalRow, VerificationRow
from app.persistence.models.base import utcnow
from app.persistence.models.run import ToolInvocationRow
from app.persistence.repositories.base import BaseRepository


class ActionRepository(BaseRepository[ActionRow, Action]):
    row_class = ActionRow
    domain_class = Action

    async def create(self, values: dict[str, Any]) -> Action:
        row = ActionRow(**values)
        await self.add(row)
        await self.session.refresh(row)
        return self.to_domain(row)

    async def update(self, action_id: str, values: dict[str, Any]) -> Action | None:
        row = await self.get_row(action_id)
        if row is None:
            return None
        self.apply(row, {"updated_at": utcnow(), **values})
        await self.session.flush()
        await self.session.refresh(row)
        return self.to_domain(row)

    async def list_for_incident(self, incident_id: str) -> list[Action]:
        result = await self.session.execute(
            select(ActionRow)
            .where(ActionRow.incident_id == incident_id)
            .order_by(ActionRow.created_at)
        )
        return self.to_domain_list(list(result.scalars().all()))

    async def get_by_idempotency_key(self, key: str) -> Action | None:
        result = await self.session.execute(
            select(ActionRow).where(ActionRow.idempotency_key == key)
        )
        row = result.scalar_one_or_none()
        return self.to_domain(row) if row else None

    async def list_awaiting_approval(self, limit: int = 100) -> list[Action]:
        result = await self.session.execute(
            select(ActionRow)
            .where(ActionRow.status == ActionStatus.AWAITING_APPROVAL.value)
            .order_by(ActionRow.created_at)
            .limit(limit)
        )
        return self.to_domain_list(list(result.scalars().all()))

    async def count_recent_rollbacks(self, since: datetime) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(ActionRow)
            .where(
                ActionRow.tool_name == "deployment.rollback_simulation",
                ActionRow.created_at >= since,
                ActionRow.status.in_([ActionStatus.SUCCEEDED.value, ActionStatus.EXECUTING.value]),
            )
        )
        return int(result.scalar_one())

    async def success_rate(self, window: int = 200) -> float:
        """Rolling action success ratio (SLO: tool execution success >= 99%)."""
        result = await self.session.execute(
            select(ActionRow.status).order_by(ActionRow.created_at.desc()).limit(window)
        )
        statuses = [str(value) for value in result.scalars().all()]
        attempted = [
            status
            for status in statuses
            if status
            in {
                ActionStatus.SUCCEEDED.value,
                ActionStatus.FAILED.value,
            }
        ]
        if not attempted:
            return 1.0
        return attempted.count(ActionStatus.SUCCEEDED.value) / len(attempted)


class ApprovalRepository(BaseRepository[ApprovalRow, Approval]):
    row_class = ApprovalRow
    domain_class = Approval

    async def create(self, values: dict[str, Any]) -> Approval:
        row = ApprovalRow(**values)
        await self.add(row)
        await self.session.refresh(row)
        return self.to_domain(row)

    async def get_for_action(self, action_id: str) -> Approval | None:
        result = await self.session.execute(
            select(ApprovalRow)
            .where(ApprovalRow.action_id == action_id)
            .order_by(ApprovalRow.requested_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return self.to_domain(row) if row else None

    async def list_pending(self, limit: int = 100) -> list[Approval]:
        result = await self.session.execute(
            select(ApprovalRow)
            .where(ApprovalRow.decision == ApprovalDecision.PENDING.value)
            .order_by(ApprovalRow.requested_at)
            .limit(limit)
        )
        return self.to_domain_list(list(result.scalars().all()))

    async def decide(
        self,
        approval_id: str,
        *,
        decision: ApprovalDecision,
        actor: str,
        role: str,
        reason: str = "",
        expected_payload_hash: str | None = None,
    ) -> Approval | None:
        """Record a human decision, refusing to decide twice or on a mismatched payload."""
        from app.core.errors import ApprovalInvalidated, ConflictError, NotFoundError

        row = await self.get_row(approval_id)
        if row is None:
            raise NotFoundError(f"Approval {approval_id} does not exist.")
        if row.decision != ApprovalDecision.PENDING.value:
            raise ConflictError(
                "This approval has already been decided.",
                details={"decision": row.decision},
            )
        if expected_payload_hash is not None and not row.payload_hash == expected_payload_hash:
            raise ApprovalInvalidated(
                "The approval does not match the action payload.",
                details={"expected": row.payload_hash, "provided": expected_payload_hash},
            )

        now = utcnow()
        row.decision = decision.value
        row.actor = actor
        row.role = role
        row.reason = reason
        row.decided_at = now
        await self.session.flush()
        await self.session.refresh(row)
        return self.to_domain(row)

    async def verify_binding(
        self,
        approval_id: str,
        payload: dict[str, Any] | None = None,
        *,
        action: Action | None = None,
    ) -> bool:
        """True when the approval is final, unexpired and bound to this exact payload.

        Either pass the payload directly or the action it belongs to; passing the action is
        preferred because it hashes ``{"tool_name", "params"}`` exactly as it was stored.
        """
        if payload is None:
            if action is None:
                raise ValueError("verify_binding requires either payload or action")
            payload = action_payload(action.tool_name, action.params)
        """True when the approval is final, unexpired and bound to this exact payload."""
        row = await self.get_row(approval_id)
        if row is None:
            return False
        if row.decision != ApprovalDecision.APPROVED.value:
            return False
        if row.expires_at < utcnow():
            return False
        return bool(row.payload_hash == canonical_hash(payload))


class VerificationRepository(BaseRepository[VerificationRow, VerificationResult]):
    row_class = VerificationRow
    domain_class = VerificationResult

    async def create(self, values: dict[str, Any]) -> VerificationResult:
        row = VerificationRow(**values)
        await self.add(row)
        await self.session.refresh(row)
        return self.to_domain(row)

    async def latest_for_incident(self, incident_id: str) -> VerificationResult | None:
        result = await self.session.execute(
            select(VerificationRow)
            .where(VerificationRow.incident_id == incident_id)
            .order_by(VerificationRow.completed_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return self.to_domain(row) if row else None

    async def list_for_incident(self, incident_id: str) -> list[VerificationResult]:
        result = await self.session.execute(
            select(VerificationRow)
            .where(VerificationRow.incident_id == incident_id)
            .order_by(VerificationRow.completed_at)
        )
        return self.to_domain_list(list(result.scalars().all()))

    async def outcome_counts(self, since: datetime) -> dict[str, int]:
        result = await self.session.execute(
            select(VerificationRow.outcome, func.count())
            .where(VerificationRow.completed_at >= since)
            .group_by(VerificationRow.outcome)
        )
        return {str(outcome): int(count) for outcome, count in result.all()}


class ToolInvocationRepository(BaseRepository[ToolInvocationRow, ToolInvocation]):
    row_class = ToolInvocationRow
    domain_class = ToolInvocation

    async def record(self, values: dict[str, Any]) -> ToolInvocation:
        row = ToolInvocationRow(**values)
        await self.add(row)
        await self.session.refresh(row)
        return self.to_domain(row)

    async def list_for_run(self, run_id: str) -> list[ToolInvocation]:
        result = await self.session.execute(
            select(ToolInvocationRow)
            .where(ToolInvocationRow.run_id == run_id)
            .order_by(ToolInvocationRow.occurred_at)
        )
        return self.to_domain_list(list(result.scalars().all()))

    async def failure_counts(self, since: datetime) -> dict[str, int]:
        result = await self.session.execute(
            select(ToolInvocationRow.tool_name, func.count())
            .where(
                ToolInvocationRow.occurred_at >= since,
                ToolInvocationRow.outcome.in_(
                    [ToolOutcome.FAILURE.value, ToolOutcome.TIMEOUT.value]
                ),
            )
            .group_by(ToolInvocationRow.tool_name)
        )
        return {str(tool): int(count) for tool, count in result.all()}

    async def recent_risk_levels(self, limit: int = 50) -> list[RiskLevel]:  # pragma: no cover
        result = await self.session.execute(
            select(ToolInvocationRow.risk)
            .order_by(ToolInvocationRow.occurred_at.desc())
            .limit(limit)
        )
        return [RiskLevel(str(value)) for value in result.scalars().all()]


__all__ = [
    "ActionRepository",
    "ApprovalRepository",
    "ToolInvocationRepository",
    "VerificationRepository",
]
