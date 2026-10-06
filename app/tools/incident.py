"""``incident.escalate`` — hand an incident to a human, deliberately.

Escalation is a *first-class outcome*, not an error path (ADR-0004). Making it a tool means
it is policy-checked, audited and observed identically to every other action, so "the agent
gave up" is as traceable as "the agent acted".
"""

from __future__ import annotations

from app.domain.enums import RiskLevel, ToolOutcome
from app.domain.tools import ToolResult, ToolSpec
from app.tools.registry import ToolContext, ToolDefinition
from app.tools.schemas import IncidentEscalateParams

SPEC = ToolSpec(
    name="incident.escalate",
    description=(
        "Escalate an incident to the on-call human with the reason recorded. "
        "Used when policy denies an action, verification fails, or evidence is insufficient."
    ),
    permission="incidents.write",
    risk=RiskLevel.MEDIUM,
    requires_approval=False,
    timeout_seconds=10.0,
    max_retries=2,
    input_schema={
        "type": "object",
        "properties": {
            "incident_id": {"type": "string", "maxLength": 64},
            "reason": {"type": "string", "maxLength": 500},
            "notify_channel": {"type": "string", "maxLength": 80},
        },
        "required": ["incident_id", "reason"],
        "additionalProperties": False,
    },
    output_schema={"type": "object", "properties": {"escalated": {"type": "boolean"}}},
    idempotent=True,
)


async def _handler(params: IncidentEscalateParams, context: ToolContext) -> ToolResult:
    data = await context.integrations.slack_notify(
        {
            "incident_id": params.incident_id,
            "text": f":rotating_light: Escalation: {params.reason}",
            "severity": "SEV2",
            "channel": params.notify_channel,
        }
    )
    delivered = bool(data.get("message_ts"))
    return ToolResult(
        tool_name=SPEC.name,
        success=delivered,
        outcome=ToolOutcome.SUCCESS if delivered else ToolOutcome.FAILURE,
        data={"escalated": delivered, "reason": params.reason, **data},
        simulated=bool(data.get("simulated", False)),
        error=None if delivered else "escalation notification could not be delivered",
    )


def escalate_tool() -> ToolDefinition:
    return ToolDefinition(spec=SPEC, params_model=IncidentEscalateParams, handler=_handler)


__all__ = ["SPEC", "escalate_tool"]
