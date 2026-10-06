"""``knowledge.base_example`` — a deterministic, side-effect-free reference lookup.

This tool exists so that the tool path itself is demonstrable without touching any real
system: it is the smallest possible registered capability, and the tests use it to prove
that policy, approval and audit work for *any* tool rather than only for the dangerous ones.
"""

from __future__ import annotations

from app.domain.enums import RiskLevel, ToolOutcome
from app.domain.tools import ToolResult, ToolSpec
from app.tools.registry import ToolContext, ToolDefinition
from app.tools.schemas import KnowledgeBaseExampleParams

KNOWLEDGE_BASE: dict[str, str] = {
    "error_rate": (
        "Error rate is the share of requests answered with a 5xx. A step change that starts "
        "minutes after a deploy is deployment-correlated until proven otherwise; a gradual "
        "rise is more often saturation or a dependency degradation."
    ),
    "latency_p95": (
        "p95 latency is sensitive to a single slow dependency. Compare against p50: if p50 is "
        "flat while p95 doubles, the cause is usually partial (one replica, one endpoint, one "
        "shard) rather than global."
    ),
    "rollback": (
        "A rollback is safe only to a concrete previously released version, and it must be "
        "verified by observing recovery, not by observing that the rollback call returned."
    ),
    "payment_failure_rate": (
        "Payment failures cluster by provider status, card BIN and plan. Always separate "
        "'provider incident' (sudden, broad) from 'integration regression' (starts with a "
        "deploy, affects one flow)."
    ),
    "deduplication": (
        "Repeat anomalies within the dedup window belong to the same incident; creating a new "
        "incident per minute pages a human thirty times for one problem."
    ),
}

SPEC = ToolSpec(
    name="knowledge.base_example",
    description=(
        "Look up a short, deterministic reference note about an operational concept. "
        "Read-only, no external system is contacted."
    ),
    permission="knowledge.read",
    risk=RiskLevel.LOW,
    requires_approval=False,
    timeout_seconds=2.0,
    max_retries=0,
    input_schema={
        "type": "object",
        "properties": {"topic": {"type": "string", "maxLength": 120}},
        "required": ["topic"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {"topic": {"type": "string"}, "note": {"type": "string"}},
    },
    idempotent=True,
    simulated=False,
)


async def _handler(params: KnowledgeBaseExampleParams, context: ToolContext) -> ToolResult:
    topic = params.topic.strip().lower()
    note = KNOWLEDGE_BASE.get(topic)
    if note is None:
        return ToolResult(
            tool_name=SPEC.name,
            success=False,
            outcome=ToolOutcome.FAILURE,
            error=f"no reference note for topic '{params.topic}'",
            data={"available_topics": sorted(KNOWLEDGE_BASE)},
        )
    return ToolResult(
        tool_name=SPEC.name,
        success=True,
        outcome=ToolOutcome.SUCCESS,
        data={"topic": topic, "note": note},
    )


def knowledge_example_tool() -> ToolDefinition:
    return ToolDefinition(spec=SPEC, params_model=KnowledgeBaseExampleParams, handler=_handler)


__all__ = ["KNOWLEDGE_BASE", "SPEC", "knowledge_example_tool"]
