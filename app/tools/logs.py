"""``logs.query`` — read-only log window retrieval."""

from __future__ import annotations

from app.domain.enums import RiskLevel, ToolOutcome
from app.domain.tools import ToolResult, ToolSpec
from app.tools.registry import ToolContext, ToolDefinition
from app.tools.schemas import LogsQueryParams

SPEC = ToolSpec(
    name="logs.query",
    description="Query recent log lines for a service, optionally filtered by severity or text.",
    permission="logs.read",
    risk=RiskLevel.LOW,
    requires_approval=False,
    timeout_seconds=10.0,
    max_retries=2,
    input_schema={
        "type": "object",
        "properties": {
            "service": {"type": "string", "maxLength": 120},
            "window_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
            "severity": {"type": "string", "maxLength": 16},
            "contains": {"type": "string", "maxLength": 200},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["service"],
        "additionalProperties": False,
    },
    output_schema={"type": "object", "properties": {"lines": {"type": "array"}}},
    idempotent=True,
)


async def _handler(params: LogsQueryParams, context: ToolContext) -> ToolResult:
    data = await context.integrations.logs_window(
        service=params.service,
        window_minutes=params.window_minutes,
        severity=params.severity,
        contains=params.contains,
        limit=params.limit,
    )
    return ToolResult(
        tool_name=SPEC.name,
        success=True,
        outcome=ToolOutcome.SUCCESS,
        data=data,
        simulated=bool(data.get("simulated", False)),
    )


def logs_query_tool() -> ToolDefinition:
    return ToolDefinition(spec=SPEC, params_model=LogsQueryParams, handler=_handler)


__all__ = ["SPEC", "logs_query_tool"]
