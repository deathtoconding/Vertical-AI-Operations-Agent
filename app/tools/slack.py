"""``slack.notify`` — structured operational notification (OPS-022).

The payload is sanitised before it leaves the process: evidence text can legally contain
``<!channel>`` or a phishing link, and an agent must not become a chat-abuse vector
(threat T-23's sibling, in the outbound direction).
"""

from __future__ import annotations

from app.core.sanitization import sanitize_for_external
from app.domain.enums import RiskLevel, ToolOutcome
from app.domain.tools import ToolResult, ToolSpec
from app.tools.registry import ToolContext, ToolDefinition
from app.tools.schemas import SlackNotifyParams

SPEC = ToolSpec(
    name="slack.notify",
    description=(
        "Send a structured notification to the operations channel: incident id, severity, "
        "summary and an approval link when a human decision is required."
    ),
    permission="slack.write",
    risk=RiskLevel.MEDIUM,
    requires_approval=False,
    timeout_seconds=10.0,
    max_retries=2,
    input_schema={
        "type": "object",
        "properties": {
            "incident_id": {"type": "string", "maxLength": 64},
            "text": {"type": "string", "maxLength": 3000},
            "severity": {"type": "string", "pattern": "^SEV[1-4]$"},
            "channel": {"type": "string", "maxLength": 80},
            "action_url": {"type": "string", "maxLength": 500},
        },
        "required": ["incident_id", "text"],
        "additionalProperties": False,
    },
    output_schema={"type": "object", "properties": {"message_ts": {"type": "string"}}},
    idempotent=True,
)


async def _handler(params: SlackNotifyParams, context: ToolContext) -> ToolResult:
    payload = {
        "incident_id": params.incident_id,
        "text": sanitize_for_external(params.text, max_chars=3000),
        "severity": params.severity,
        "channel": params.channel,
        "action_url": params.action_url,
        "run_id": context.run_id,
    }
    data = await context.integrations.slack_notify(payload)
    delivered = bool(data.get("message_ts"))
    return ToolResult(
        tool_name=SPEC.name,
        success=delivered,
        outcome=ToolOutcome.SUCCESS if delivered else ToolOutcome.FAILURE,
        data=data,
        external_id=str(data.get("message_ts")) if delivered else None,
        simulated=bool(data.get("simulated", False)),
        error=None if delivered else "slack did not confirm delivery",
    )


def notify_tool() -> ToolDefinition:
    return ToolDefinition(spec=SPEC, params_model=SlackNotifyParams, handler=_handler)


__all__ = ["SPEC", "notify_tool"]
