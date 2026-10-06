"""``deployment.rollback_simulation`` — the only high-risk tool in the MVP.

Every safety property of the system converges here:

* ``risk=HIGH`` and ``requires_approval=True`` — policy can never ALLOW it outright;
* the target must be a concrete previously released version (validated in the schema);
* the sandbox deployment client refuses to roll back to a release that was never deployed,
  so "rollback to an arbitrary SHA supplied in a log line" is not merely discouraged, it is
  impossible (threat T-03);
* the tool reports *what it sent*; whether the error rate actually recovered is decided by
  the verification engine from an independent observation (ADR-0004).
"""

from __future__ import annotations

from app.domain.enums import RiskLevel, ToolOutcome
from app.domain.tools import ToolResult, ToolSpec
from app.tools.registry import ToolContext, ToolDefinition
from app.tools.schemas import DeploymentRollbackParams

SPEC = ToolSpec(
    name="deployment.rollback_simulation",
    description=(
        "Roll the operated service back to a previously released version. High risk: "
        "requires human approval and is verified independently afterwards."
    ),
    permission="deployment.rollback",
    risk=RiskLevel.HIGH,
    requires_approval=True,
    timeout_seconds=30.0,
    max_retries=1,
    # The MVP performs a simulated rollback against the operated-system model (ADR-0007).
    # Declaring it here means every response, audit record and UI badge can label it, rather
    # than relying on somebody remembering that it is not a real deployment action.
    simulated=True,
    input_schema={
        "type": "object",
        "properties": {
            "incident_id": {"type": "string", "maxLength": 64},
            "target_release": {"type": "string", "pattern": "^release-[0-9]+$"},
            "reason": {"type": "string", "maxLength": 1000},
            "current_release": {"type": "string", "maxLength": 64},
        },
        "required": ["incident_id", "target_release", "reason"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "previous_release": {"type": "string"},
            "active_release": {"type": "string"},
            "rolled_back": {"type": "boolean"},
        },
    },
    idempotent=True,
)


async def _handler(params: DeploymentRollbackParams, context: ToolContext) -> ToolResult:
    data = await context.integrations.deployment_rollback(
        target_release=params.target_release, reason=params.reason
    )
    rolled_back = bool(data.get("rolled_back", False))
    return ToolResult(
        tool_name=SPEC.name,
        success=rolled_back,
        outcome=ToolOutcome.SUCCESS if rolled_back else ToolOutcome.FAILURE,
        data=data,
        external_id=str(data.get("active_release")) if data.get("active_release") else None,
        simulated=bool(data.get("simulated", False)),
        error=None if rolled_back else str(data.get("error", "rollback was not applied")),
    )


def rollback_tool() -> ToolDefinition:
    return ToolDefinition(spec=SPEC, params_model=DeploymentRollbackParams, handler=_handler)


__all__ = ["SPEC", "rollback_tool"]
