"""Interrupted-run recovery (OPS-002 AC, docs/architecture/agent-runtime.md §4).

A crashed worker leaves a run in a non-terminal state. Recovery is safe by construction:

1. only runs whose lease has expired are touched;
2. the interrupted stage is *recorded*, never silently skipped;
3. a run interrupted in ``EXECUTING`` is **not** re-executed. Its high-risk action needs a
   fresh human decision — the run moves to ``WAITING_APPROVAL`` instead, because a retry that
   re-fires a rollback is exactly the duplicate-side-effect failure this path exists to avoid;
4. everything else resumes from persisted state.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.core.security import Actor
from app.core.telemetry import AGENT_RUN_RECOVERED
from app.domain.enums import ActionStatus, AgentState, AuditEventType

logger = get_logger(__name__)


async def recover_interrupted_runs(
    container: Any, services: Any, *, actor: Actor
) -> dict[str, Any]:
    settings = container.settings
    interrupted = await services.runs.list_interrupted(settings.run_lease_seconds)

    recovered: list[dict[str, Any]] = []
    for run in interrupted:
        decision = "resumed"
        target_state = run.state

        if run.state is AgentState.EXECUTING:
            # Do not replay a side effect. Park the run for a human instead.
            target_state = AgentState.WAITING_APPROVAL
            decision = "parked_for_approval"
            actions = await services.actions.list_for_incident(run.incident_id)
            for action in actions:
                if action.status is ActionStatus.EXECUTING:
                    await services.actions.update(
                        action.id,
                        {
                            "status": ActionStatus.FAILED.value,
                            "error": "run interrupted during execution; not retried automatically",
                        },
                    )
        elif run.state in {AgentState.FAILED, AgentState.ESCALATED}:
            decision = "left_for_human"

        if decision == "resumed" and target_state is not run.state:
            await services.runs.transition(
                run.id,
                target_state,
                actor=actor.actor_id,
                reason="recovered after lease expiry; not re-executed",
                expected_version=run.version,
            )

        AGENT_RUN_RECOVERED.inc()
        await services.audit.append(
            AuditEventType.RUN_RECOVERED,
            actor=actor.actor_id,
            role=actor.role.value,
            incident_id=run.incident_id,
            agent_run_id=run.id,
            outcome=decision,
            reason=f"interrupted in {run.state.value}; {decision}",
            payload={"target_state": target_state.value},
        )
        recovered.append(
            {
                "run_id": run.id,
                "incident_id": run.incident_id,
                "from_state": run.state.value,
                "decision": decision,
            }
        )

    logger.info("recovery_completed", scanned=len(interrupted), recovered=len(recovered))
    return {
        "scanned": len(interrupted),
        "recovered": recovered,
        "policy": "high-risk actions are never re-executed without a fresh approval",
    }


__all__ = ["recover_interrupted_runs"]
