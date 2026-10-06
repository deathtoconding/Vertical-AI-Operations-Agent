"""``jira.create_incident`` — idempotent ticket creation (OPS-021).

Idempotency is enforced here *and* in the integration: the executor supplies the derived key
and the Jira client refuses to create a second issue for the same key. A retry after a
timeout therefore returns the original issue instead of a duplicate ticket (which is how
incident trackers become untrustworthy).
"""

from __future__ import annotations

from app.core.sanitization import sanitize_for_external
from app.domain.enums import RiskLevel, ToolOutcome
from app.domain.tools import ToolResult, ToolSpec
from app.integrations.idempotency import jira_issue_key
from app.tools.registry import ToolContext, ToolDefinition
from app.tools.schemas import JiraCreateIncidentParams

SPEC = ToolSpec(
    name="jira.create_incident",
    description=(
        "Create (or replay) a Jira issue describing an incident, with its evidence summary "
        "and a link back to the agent run. Idempotent per incident payload."
    ),
    permission="jira.write",
    risk=RiskLevel.MEDIUM,
    requires_approval=False,
    timeout_seconds=15.0,
    max_retries=2,
    input_schema={
        "type": "object",
        "properties": {
            "incident_id": {"type": "string", "maxLength": 64},
            "summary": {"type": "string", "maxLength": 250},
            "description": {"type": "string", "maxLength": 8000},
            "priority": {"type": "string", "pattern": "^P[0-4]$"},
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
            "labels": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["incident_id", "summary"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {"issue_key": {"type": "string"}, "replayed": {"type": "boolean"}},
    },
    idempotent=True,
)


async def _handler(params: JiraCreateIncidentParams, context: ToolContext) -> ToolResult:
    payload = {
        "incident_id": params.incident_id,
        "summary": sanitize_for_external(params.summary, max_chars=250),
        "description": sanitize_for_external(params.description, max_chars=8000),
        "priority": params.priority,
        "evidence_ids": params.evidence_ids[:50],
        "labels": params.labels[:10],
        "run_id": context.run_id,
    }
    # Derived from the payload, not from the attempt: a timeout retry must return the
    # original issue, and a genuinely different incident payload must create a new one.
    key = jira_issue_key(params.incident_id, payload)
    data = await context.integrations.jira_create_issue(payload, idempotency_key=key)
    return ToolResult(
        tool_name=SPEC.name,
        success=bool(data.get("issue_key")),
        outcome=ToolOutcome.SUCCESS if data.get("issue_key") else ToolOutcome.FAILURE,
        data=data,
        external_id=str(data.get("issue_key")) if data.get("issue_key") else None,
        replayed=bool(data.get("replayed", False)),
        simulated=bool(data.get("simulated", False)),
        error=None if data.get("issue_key") else "jira did not return an issue key",
    )


def create_incident_tool() -> ToolDefinition:
    return ToolDefinition(spec=SPEC, params_model=JiraCreateIncidentParams, handler=_handler)


__all__ = ["SPEC", "create_incident_tool"]
