"""``metrics.query`` — read-only metric window retrieval."""

from __future__ import annotations

from app.domain.enums import RiskLevel, ToolOutcome
from app.domain.tools import ToolResult, ToolSpec
from app.tools.registry import ToolContext, ToolDefinition
from app.tools.schemas import MetricsQueryParams

SPEC = ToolSpec(
    name="metrics.query",
    description="Query a metric series for a service over a bounded window.",
    permission="metrics.read",
    risk=RiskLevel.LOW,
    requires_approval=False,
    timeout_seconds=10.0,
    max_retries=2,
    input_schema={
        "type": "object",
        "properties": {
            "service": {"type": "string", "maxLength": 120},
            "metric": {"type": "string", "maxLength": 64},
            "window_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
        },
        "required": ["service", "metric"],
        "additionalProperties": False,
    },
    output_schema={"type": "object", "properties": {"points": {"type": "array"}}},
    idempotent=True,
)


async def _handler(params: MetricsQueryParams, context: ToolContext) -> ToolResult:
    data = await context.integrations.metrics_window(
        params.service, params.metric, params.window_minutes
    )
    return ToolResult(
        tool_name=SPEC.name,
        success=True,
        outcome=ToolOutcome.SUCCESS,
        data=data,
        simulated=bool(data.get("simulated", False)),
    )


def metrics_query_tool() -> ToolDefinition:
    return ToolDefinition(spec=SPEC, params_model=MetricsQueryParams, handler=_handler)


__all__ = ["SPEC", "metrics_query_tool"]
