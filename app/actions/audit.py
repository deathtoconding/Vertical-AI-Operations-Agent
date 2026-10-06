"""Audit helpers for the action lifecycle (SEC-004).

Action-related audit events always carry the same context — incident, run, tool, action,
approval. Passing those five fields by hand at thirteen call sites is how one of them ends up
missing and the trail develops a hole, so the executor goes through this helper instead.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.domain.actions import Action
from app.domain.enums import AuditEventType

logger = get_logger(__name__)


class ActionAuditor:
    """Append action-lifecycle events with their context attached."""

    def __init__(self, audit: Any) -> None:
        self.audit = audit

    async def record(
        self,
        event: AuditEventType,
        action: Action,
        *,
        actor: str,
        role: str = "system",
        outcome: str = "info",
        reason: str = "",
        approval_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        enriched = {
            "tool_name": action.tool_name,
            "risk": action.risk.value,
            "policy_decision": action.policy_decision,
            "payload_hash": action.canonical_hash[:32],
            **(payload or {}),
        }
        await self.audit.append(
            event,
            actor=actor,
            role=role,
            incident_id=action.incident_id,
            agent_run_id=action.run_id,
            tool_name=action.tool_name,
            action_id=action.id,
            approval_id=approval_id,
            outcome=outcome[:16],
            reason=reason[:4000],
            payload=enriched,
        )

    async def record_standalone(
        self,
        event: AuditEventType,
        *,
        actor: str,
        role: str = "system",
        incident_id: str | None = None,
        run_id: str | None = None,
        tool_name: str | None = None,
        outcome: str = "info",
        reason: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        """For refusals that happen before an action row exists (e.g. unknown tool)."""
        await self.audit.append(
            event,
            actor=actor,
            role=role,
            incident_id=incident_id,
            agent_run_id=run_id,
            tool_name=tool_name,
            outcome=outcome[:16],
            reason=reason[:4000],
            payload=payload or {},
        )


__all__ = ["ActionAuditor"]
