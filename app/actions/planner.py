"""Turn a diagnosis into policy-evaluated action proposals (OPS-060).

The planner is where model output meets the deterministic world. Everything here is designed
around one assumption: **the proposal may be nonsense**. It may name a tool that does not
exist, ask for a parameter that would break the operated service, or cite evidence it never
saw. The planner's job is to convert the usable parts into concrete requests, drop the rest
*visibly*, and pre-declare what "fixed" will look like before anything runs.

That declaration matters more than it looks: ``ExpectedState`` is what verification later
compares against. Deriving it after execution would let the system "verify" that whatever
happened was what was intended.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings
from app.domain.actions import ActionRequest, ExpectedState

#: Metrics whose recovery means an API incident is over, and the value that counts as recovered.
RECOVERY_TARGETS: dict[str, float] = {
    "error_rate": 0.02,
    "latency_p95": 400.0,
    "payment_failure_rate": 0.02,
}

#: Tools whose success is observable by reading the target system back.
VERIFICATION_CHECKS: dict[str, list[dict[str, Any]]] = {
    "deployment.rollback_simulation": [
        {"check": "release_active"},
        {"check": "deployment_healthy"},
        {"check": "metric_recovered"},
    ],
    "slack.notify": [{"check": "notification_delivered"}],
    "jira.create_incident": [{"check": "issue_exists"}],
}


@dataclass
class PlannedActions:
    """The planner's output: what will be attempted, and what was refused."""

    requests: list[ActionRequest] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    rationale: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.requests


def expected_state_for(
    tool_name: str,
    params: dict[str, Any],
    *,
    metric: str | None = None,
    settings: Settings | None = None,
) -> ExpectedState:
    """Declare what the world should look like if this action worked.

    A tool with no meaningful post-condition returns an empty expectation rather than a
    fabricated one; verification then reports ``UNKNOWN`` instead of inventing a pass.
    """
    checks = VERIFICATION_CHECKS.get(tool_name)
    if not checks:
        return ExpectedState()

    state = ExpectedState(
        checks=[dict(check) for check in checks],
        window_seconds=settings.verification_window_seconds if settings else None,
    )
    if tool_name == "deployment.rollback_simulation":
        target_metric = metric or "error_rate"
        state.metric = target_metric
        state.threshold = RECOVERY_TARGETS.get(target_metric)
        state.comparison = "below"
        state.target_release = str(params.get("target_release") or "")
        state.description = (
            f"{target_metric} recovers below the declared target and "
            f"{state.target_release or 'the target release'} is active"
        )
    elif tool_name == "jira.create_incident":
        state.description = "the escalation issue exists in the tracker"
    else:
        state.description = "the notification is present in the channel"
    return state


def plan_actions(
    diagnosis: dict[str, Any],
    *,
    incident_id: str,
    metric: str | None = None,
    settings: Settings | None = None,
    known_tools: frozenset[str] | None = None,
) -> PlannedActions:
    """Convert ``Diagnosis.recommended_actions`` into ordered, validated requests.

    ``known_tools`` is supplied by the caller from the registry; when a hint is not in it the
    proposal is rejected *here*, with a reason, rather than surfacing later as a confusing
    "unknown tool" denial. A hallucinated capability should look like a refused proposal, not
    like an outage.
    """
    plan = PlannedActions(rationale=str(diagnosis.get("hypothesis") or ""))
    diagnosis_evidence = [str(item) for item in (diagnosis.get("evidence_ids") or [])]

    for index, proposal in enumerate(diagnosis.get("recommended_actions") or []):
        tool_name = str(proposal.get("tool_hint") or "").strip()
        params = dict(proposal.get("params") or {})

        if not tool_name:
            plan.rejected.append(
                {"index": index, "intent": proposal.get("intent"), "reason": "no tool hint"}
            )
            continue
        if known_tools is not None and tool_name not in known_tools:
            plan.rejected.append(
                {
                    "index": index,
                    "tool_name": tool_name,
                    "reason": "tool is not registered; the proposal cannot be executed",
                }
            )
            continue

        evidence_ids = [str(item) for item in (proposal.get("evidence_ids") or diagnosis_evidence)]
        plan.requests.append(
            ActionRequest(
                incident_id=incident_id,
                tool_name=tool_name,
                params=params,
                rationale=str(proposal.get("rationale") or ""),
                evidence_ids=evidence_ids,
                expected_state=expected_state_for(
                    tool_name, params, metric=metric, settings=settings
                ),
                sequence=index,
            )
        )

    return plan


__all__ = [
    "RECOVERY_TARGETS",
    "VERIFICATION_CHECKS",
    "PlannedActions",
    "expected_state_for",
    "plan_actions",
]
