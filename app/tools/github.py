"""``github.read_commits`` — read-only deployment correlation.

Read-only by construction: the adapter it calls has no write method, and the token scope is
documented as read-only (threat T-30).
"""

from __future__ import annotations

from app.domain.enums import RiskLevel, ToolOutcome
from app.domain.tools import ToolResult, ToolSpec
from app.tools.registry import ToolContext, ToolDefinition
from app.tools.schemas import GitHubReadCommitsParams

SPEC = ToolSpec(
    name="github.read_commits",
    description=(
        "List recent commits, pull requests and deployments for the operated repository. "
        "Used to correlate an incident onset with a release."
    ),
    permission="github.read",
    risk=RiskLevel.LOW,
    requires_approval=False,
    timeout_seconds=10.0,
    max_retries=2,
    input_schema={
        "type": "object",
        "properties": {
            "repository": {"type": "string", "maxLength": 200},
            "since_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "branch": {"type": "string", "maxLength": 120},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "object", "properties": {"commits": {"type": "array"}}},
    idempotent=True,
)


async def _handler(params: GitHubReadCommitsParams, context: ToolContext) -> ToolResult:
    data = await context.integrations.github_commits(
        repository=params.repository,
        since_minutes=params.since_minutes,
        limit=params.limit,
        branch=params.branch,
    )
    return ToolResult(
        tool_name=SPEC.name,
        success=True,
        outcome=ToolOutcome.SUCCESS,
        data=data,
        simulated=bool(data.get("simulated", False)),
    )


def read_commits_tool() -> ToolDefinition:
    return ToolDefinition(spec=SPEC, params_model=GitHubReadCommitsParams, handler=_handler)


__all__ = ["SPEC", "read_commits_tool"]
