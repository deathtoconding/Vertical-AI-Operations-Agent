"""The incident orchestrator (OPS-050 / OPS-060..063).

One method, :meth:`IncidentOrchestrator.handle_incident`, advances one incident as far as the
state machine and policy allow, then stops and reports honestly where it stopped:

```
DETECTED -> INVESTIGATING -> PLANNED -> [WAITING_APPROVAL] -> EXECUTING -> VERIFYING -> RESOLVED
                                                                              \\-> ESCALATED
```

Three properties are load-bearing:

* **Pausing is normal.** ``WAITING_APPROVAL`` is a successful outcome of a step, not an error.
  The caller gets ``requires_human=True`` and the approval id; a human decision resumes the run
  through :meth:`resume` without re-investigating (the diagnosis is persisted).
* **The run owns durable state.** Stage progress is written to ``agent_runs`` before each stage
  completes, so a crash resumes rather than restarts.
* **Nothing is claimed that was not observed.** Resolution requires a ``SUCCESS`` verification;
  ``FAILED`` and ``UNKNOWN`` both escalate, because an unverified recovery is not a recovery.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.actions.approval import build_approval_request
from app.actions.executor import ActionExecutor
from app.actions.planner import expected_state_for, plan_actions
from app.agent.state_machine import assert_transition, record_transition_audit
from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.security import Actor
from app.core.telemetry import (
    AGENT_RUN_DURATION,
    AGENT_RUN_FAILURES,
    APPROVALS_REQUESTED,
    ESCALATIONS,
)
from app.domain.actions import ActionRequest
from app.domain.enums import (
    ActionStatus,
    AgentState,
    ApprovalDecision,
    AuditEventType,
    IncidentStatus,
    Severity,
    VerificationOutcome,
)
from app.domain.incidents import Incident
from app.investigation.collector import EvidenceCollector
from app.investigation.investigator import Investigator
from app.persistence.models.base import new_prefixed_id, utcnow
from app.persistence.repositories.actions import (
    ActionRepository,
    ApprovalRepository,
    VerificationRepository,
)
from app.persistence.repositories.agent_runs import AgentRunRepository
from app.persistence.repositories.audit import AuditRepository
from app.persistence.repositories.evidence import EvidenceRepository
from app.persistence.repositories.incidents import AnomalyRepository, IncidentRepository
from app.policy.engine import PolicyEngine
from app.tools.registry import ToolRegistry
from app.verification.engine import VerificationEngine

logger = get_logger(__name__)

#: Actions above this severity threshold must be notified to a human, always.
NOTIFY_FROM_SEVERITY = Severity.SEV3


class IncidentOrchestrator:
    """Advance incidents through the workflow."""

    def __init__(
        self,
        settings: Settings,
        *,
        registry: ToolRegistry,
        policy: PolicyEngine,
        executor: ActionExecutor,
        collector: EvidenceCollector,
        investigator: Investigator,
        verifier: VerificationEngine,
        incidents: IncidentRepository,
        anomalies: AnomalyRepository,
        evidence: EvidenceRepository,
        runs: AgentRunRepository,
        actions: ActionRepository,
        approvals: ApprovalRepository,
        verifications: VerificationRepository,
        audit: AuditRepository,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.policy = policy
        self.executor = executor
        self.collector = collector
        self.investigator = investigator
        self.verifier = verifier
        self.incidents = incidents
        self.anomalies = anomalies
        self.evidence = evidence
        self.runs = runs
        self.actions = actions
        self.approvals = approvals
        self.verifications = verifications
        self.audit = audit

    # ------------------------------------------------------------------ #
    # Entry points
    # ------------------------------------------------------------------ #

    async def handle_incident(
        self, incident_id: str, *, actor: Actor, autonomy: Any = None
    ) -> dict[str, Any]:
        """Investigate, plan, (maybe) execute and verify. Returns a snapshot dict."""
        incident = await self.incidents.get(incident_id)
        if incident is None:
            raise ValueError(f"incident {incident_id} does not exist")

        run = await self.runs.get_for_incident(incident.id)
        if run is None:
            run = await self.runs.create(
                {
                    "id": new_prefixed_id("RUN"),
                    "incident_id": incident.id,
                    "state": AgentState.NEW.value,
                    "stage": "created",
                    "actor": actor.actor_id,
                    "autonomy_level": (autonomy or self.settings.autonomy_level).value,
                    "simulated": incident.simulated,
                }
            )

            await self.incidents.update(incident.id, {"agent_run_id": run.id})
            await self.audit.append(
                AuditEventType.RUN_STATE_CHANGED,
                actor=actor.actor_id,
                role=actor.role.value,
                incident_id=incident.id,
                agent_run_id=run.id,
                outcome="created",
                reason="run created for incident",
                payload={"state": run.state.value},
            )

        if run.state.is_terminal:
            return await self._snapshot(run, stage_note="run already finished")

        started = utcnow()
        try:
            run, note = await self._advance(run, incident, actor=actor)
        except AppError as exc:
            # A typed application error is a stage failure, not a crash: escalate with cause.
            logger.warning("orchestration_failed", incident_id=incident.id, error=str(exc))
            run = await self._escalate(run, incident, actor=actor, reason=str(exc))
            note = f"escalated: {exc}"
        AGENT_RUN_DURATION.labels(state=run.state.value).observe(
            (utcnow() - started).total_seconds()
        )
        if run.state is AgentState.ESCALATED:
            AGENT_RUN_FAILURES.labels(stage=run.stage or "unknown").inc()

        snapshot = await self._snapshot(run, stage_note=note)
        logger.info(
            "incident_handled",
            incident_id=incident.id,
            run_id=run.id,
            state=run.state.value,
            note=note,
        )
        return snapshot

    async def resume(self, run_id: str, *, actor: Actor) -> dict[str, Any]:
        """Continue a run that was waiting for approval or was escalated by a human."""
        run = await self.runs.get(run_id)
        if run is None:
            raise ValueError(f"run {run_id} does not exist")
        incident = await self.incidents.get(run.incident_id)
        if incident is None:
            raise ValueError(f"incident {run.incident_id} does not exist")

        if run.state is AgentState.ESCALATED:
            # A human can hand an escalated run back with new information.
            assert_transition(AgentState.ESCALATED, AgentState.INVESTIGATING)
            await record_transition_audit(
                self.audit,
                from_state=AgentState.ESCALATED,
                to_state=AgentState.INVESTIGATING,
                run_id=run.id,
                incident_id=incident.id,
                actor=actor.actor_id,
                role=actor.role.value,
                reason="human resumed the run",
            )
            run, _ = await self.runs.transition(
                run.id,
                AgentState.INVESTIGATING,
                actor=actor.actor_id,
                reason="human resumed the run",
                expected_version=run.version,
            )
        return await self.handle_incident(incident.id, actor=actor)

    # ------------------------------------------------------------------ #
    # Stage machine
    # ------------------------------------------------------------------ #

    async def _advance(self, run: Any, incident: Incident, *, actor: Actor) -> tuple[Any, str]:
        notes: list[str] = []

        if run.state is AgentState.NEW:
            run = await self._transition(
                run, AgentState.DETECTED, actor=actor, reason="detection recorded"
            )
            notes.append("detection recorded")
        elif run.state is AgentState.RESOLVED:
            return run, "incident already resolved"

        if run.state is AgentState.DETECTED:
            run = await self._transition(
                run, AgentState.INVESTIGATING, actor=actor, reason="evidence collection started"
            )

        if run.state is AgentState.INVESTIGATING:
            run, investigation_note, diagnosis = await self._investigate(run, incident, actor=actor)
            notes.append(investigation_note)
            if diagnosis is None:
                run = await self._escalate(run, incident, actor=actor, reason=investigation_note)
                return run, " | ".join(notes)
            run = await self._transition(
                run, AgentState.PLANNED, actor=actor, reason="diagnosis produced"
            )

        if run.state is AgentState.PLANNED:
            run, plan_note, requires_approval = await self._plan(run, incident, actor=actor)
            notes.append(plan_note)
            if requires_approval:
                run = await self._transition(
                    run,
                    AgentState.WAITING_APPROVAL,
                    actor=actor,
                    reason="plan contains an action that requires human approval",
                )
                return run, " | ".join(notes)
            run = await self._transition(
                run,
                AgentState.EXECUTING,
                actor=actor,
                reason="all planned actions are permitted without approval",
                guard={"plan_has_approval_gate": False},
            )
            notes.append("no approval was required by policy")

        if run.state is AgentState.WAITING_APPROVAL:
            approved = await self._approved_action(run.id)
            if approved is None:
                return run, " | ".join([*notes, "waiting for a human decision"])
            run = await self._transition(
                run, AgentState.EXECUTING, actor=actor, reason="human approval recorded"
            )

        if run.state is AgentState.EXECUTING:
            run, execute_note, executed_action = await self._execute(run, incident, actor=actor)
            notes.append(execute_note)
            if executed_action is None:
                run = await self._escalate(run, incident, actor=actor, reason=execute_note)
                return run, " | ".join(notes)
            run = await self._transition(
                run, AgentState.VERIFYING, actor=actor, reason="execution finished"
            )

        if run.state is AgentState.VERIFYING:
            run, verify_note = await self._verify(run, incident, actor=actor)
            notes.append(verify_note)
            if run.state is AgentState.RESOLVED:
                return run, " | ".join(notes)
            return run, " | ".join(notes)

        return run, " | ".join(notes) if notes else "nothing to do"

    # ------------------------------------------------------------------ #
    # Stages
    # ------------------------------------------------------------------ #

    async def _investigate(
        self, run: Any, incident: Incident, *, actor: Actor
    ) -> tuple[Any, str, Any]:
        report = await self.collector.collect(incident)
        persisted = await self.evidence.persist(incident.id, report.drafts)
        await self.incidents.update(
            incident.id,
            {"evidence_count": await self.evidence.count_for_incident(incident.id)},
        )
        await self.audit.append(
            AuditEventType.EVIDENCE_COLLECTED,
            actor=actor.actor_id,
            role=actor.role.value,
            incident_id=incident.id,
            agent_run_id=run.id,
            outcome="collected",
            reason=f"{len(persisted)} evidence items from {len(report.sources_queried)} sources",
            payload={
                "sources": report.sources_queried,
                "items": len(persisted),
                "degradations": report.degradations,
            },
        )
        for degradation in report.degradations:
            await self.audit.append(
                AuditEventType.EVIDENCE_SOURCE_DEGRADED,
                actor=actor.actor_id,
                role=actor.role.value,
                incident_id=incident.id,
                agent_run_id=run.id,
                outcome="degraded",
                reason=str(degradation.get("detail") or degradation.get("reason") or "")[:300],
                payload=degradation,
            )

        anomaly = await self._detection_context(incident)
        diagnosis = await self.investigator.investigate(
            incident,
            persisted,
            anomaly=anomaly,
            degradations=report.degradations,
        )
        await self.incidents.update(incident.id, {"diagnosis": diagnosis.model_dump(mode="json")})
        updated = await self.runs.note(
            run.id,
            {
                "stage": "investigated",
                "diagnosis": diagnosis.model_dump(mode="json"),
                "reasoner": diagnosis.reasoner,
                "degraded_reason": diagnosis.degraded_reason,
            },
        )
        run = updated or run
        await self.audit.append(
            AuditEventType.INVESTIGATION_COMPLETED,
            actor=actor.actor_id,
            role=actor.role.value,
            incident_id=incident.id,
            agent_run_id=run.id,
            outcome="completed",
            reason=diagnosis.hypothesis[:400],
            payload={
                "reasoner": diagnosis.reasoner,
                "confidence": diagnosis.confidence,
                "evidence_ids": diagnosis.evidence_ids,
                "counter_evidence_ids": diagnosis.counter_evidence_ids,
                "injection_flags": diagnosis.injection_flags,
                "recommended_actions": [item.tool_hint for item in diagnosis.recommended_actions],
            },
        )
        if diagnosis.injection_flags:
            await self.audit.append(
                AuditEventType.PROMPT_INJECTION_DETECTED,
                actor=actor.actor_id,
                role=actor.role.value,
                incident_id=incident.id,
                agent_run_id=run.id,
                outcome="flagged",
                reason="instruction-like content found in untrusted evidence; treated as data",
                payload={"patterns": diagnosis.injection_flags},
            )
        note = (
            f"diagnosed via {diagnosis.reasoner} at confidence {diagnosis.confidence:.2f} "
            f"from {len(persisted)} evidence items"
        )
        return run, note, diagnosis

    async def _detection_context(self, incident: Incident) -> dict[str, Any]:
        records = await self.anomalies.list_for_incident(incident.id)
        if not records:
            return {}
        latest = records[-1]
        return {
            "metric": latest.metric,
            "baseline": latest.baseline,
            "observed": latest.observed,
            "deviation": latest.deviation,
            "z_score": latest.z_score,
            "relative_deviation": latest.relative_deviation,
            "sample_count": latest.sample_count,
            "severity": latest.severity.value,
            "direction": "up" if latest.deviation >= 0 else "down",
        }

    async def _plan(self, run: Any, incident: Incident, *, actor: Actor) -> tuple[Any, str, bool]:
        diagnosis_payload = run.diagnosis or incident.diagnosis or {}

        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        prior_rollbacks = await self.actions.count_recent_rollbacks(utcnow() - timedelta(hours=1))
        requires_approval = False
        approval_ids: list[str] = []

        plan_input = plan_actions(
            diagnosis_payload,
            incident_id=incident.id,
            metric=incident.metric,
            settings=self.settings,
            known_tools=frozenset(self.registry.names()),
        )
        for item in plan_input.rejected:
            # A refused proposal is information about the model's behaviour, so it is audited
            # rather than dropped: repeated refusals are how a bad prompt gets noticed.
            rejected.append(item)
            await self.audit.append(
                AuditEventType.ACTION_PROPOSAL_REJECTED,
                actor=actor.actor_id,
                role=actor.role.value,
                incident_id=incident.id,
                agent_run_id=run.id,
                tool_name=str(item.get("tool_name") or "")[:64] or None,
                outcome="rejected",
                reason=str(item.get("reason") or "proposal rejected")[:300],
                payload={"intent": item.get("intent"), "index": item.get("index")},
            )

        for request in plan_input.requests:
            action, decision, notes = await self.executor.prepare(
                request,
                actor=actor,
                incident=incident,
                run_id=run.id,
                prior_rollbacks=prior_rollbacks,
            )
            if action is None:
                rejected.append(
                    {
                        "tool_name": request.tool_name,
                        "decision": decision.decision.value,
                        "reason": decision.reason,
                        "notes": notes,
                    }
                )
                continue

            accepted.append(
                {
                    "action_id": action.id,
                    "tool_name": action.tool_name,
                    "risk": action.risk.value,
                    "status": action.status.value,
                    "policy_decision": action.policy_decision,
                    "expected_state": request.expected_state.model_dump(mode="json"),
                }
            )
            if action.requires_approval:
                requires_approval = True
                request_row = build_approval_request(
                    action,
                    approval_id=new_prefixed_id("APR"),
                    timeout_seconds=self.settings.approval_timeout_seconds,
                )
                approval = await self.approvals.create(request_row.values)
                approval_ids.append(approval.id)
                APPROVALS_REQUESTED.labels(risk=action.risk.value).inc()
                await self.audit.append(
                    AuditEventType.APPROVAL_REQUESTED,
                    actor=actor.actor_id,
                    role=actor.role.value,
                    incident_id=incident.id,
                    agent_run_id=run.id,
                    tool_name=action.tool_name,
                    action_id=action.id,
                    approval_id=approval.id,
                    outcome="pending",
                    reason=action.policy_reason,
                    payload={
                        "risk": action.risk.value,
                        "payload_hash": action.canonical_hash[:32],
                        "expires_at": approval.expires_at.isoformat(),
                    },
                )

        await self._ensure_notification(run, incident, actor=actor, accepted=accepted)
        plan = {
            "accepted": accepted,
            "rejected": rejected,
            "approval_ids": approval_ids,
            "requires_approval": requires_approval,
            "rationale": diagnosis_payload.get("hypothesis", ""),
        }
        updated = await self.runs.note(run.id, {"stage": "planned", "plan": plan})
        run = updated or run

        note = f"plan: {len(accepted)} accepted, {len(rejected)} rejected" + (
            ", approval required" if requires_approval else ""
        )
        return run, note, requires_approval

    async def _ensure_notification(
        self, run: Any, incident: Incident, *, actor: Actor, accepted: list[dict[str, Any]]
    ) -> None:
        """A SEV1/SEV2 incident always notifies a human, whether or not the model proposed it.

        Notification is not a remediation, so it is not the model's decision to remember. If
        the planner did not propose ``slack.notify`` for a serious incident, the orchestrator
        adds it — policy still authorises it like any other action.
        """
        if incident.severity.rank < NOTIFY_FROM_SEVERITY.rank:
            return
        if any(item["tool_name"] == "slack.notify" for item in accepted):
            return
        existing = await self.actions.list_for_incident(incident.id)
        if any(item.tool_name == "slack.notify" for item in existing):
            return

        request = ActionRequest(
            incident_id=incident.id,
            tool_name="slack.notify",
            params={
                "text": (
                    f"[{incident.severity.value}] {incident.title} — investigating (run {run.id})"
                ),
                "incident_id": incident.id,
                "severity": incident.severity.value,
            },
            rationale="serious incidents always notify a human, independent of the model's plan",
            evidence_ids=[],
            expected_state=expected_state_for("slack.notify", {}, settings=self.settings),
            sequence=99,
        )
        action, decision, notes = await self.executor.prepare(
            request, actor=actor, incident=incident, run_id=run.id
        )
        if action is None:
            await self.audit.append(
                AuditEventType.ACTION_PROPOSAL_REJECTED,
                actor=actor.actor_id,
                role=actor.role.value,
                incident_id=incident.id,
                agent_run_id=run.id,
                outcome="rejected",
                reason=f"mandatory notification was not permitted: {decision.reason}",
            )
            logger.warning("mandatory_notification_denied", reason=decision.reason)
            return
        accepted.append(
            {
                "action_id": action.id,
                "tool_name": action.tool_name,
                "risk": action.risk.value,
                "status": action.status.value,
                "policy_decision": action.policy_decision,
                "added_by": "orchestrator",
            }
        )
        _ = notes

    async def _ensure_issue(self, run: Any, incident: Incident, *, actor: Actor) -> Any | None:
        """Escalations get a tracked issue, so a human takes over with the record attached.

        Proposed through the same policy gate as everything else — the agent never writes to a
        tracker on the strength of its own decision.
        """
        if incident.jira_issue_key:
            return None
        existing = await self.actions.list_for_incident(incident.id)
        if any(item.tool_name == "jira.create_incident" for item in existing):
            return None

        request = ActionRequest(
            incident_id=incident.id,
            tool_name="jira.create_incident",
            params={
                "summary": f"[{incident.severity.value}] {incident.title}"[:200],
                "description": (
                    f"Escalated by the operations agent.\n\n"
                    f"Escalation reason: {incident.escalation_reason or 'unspecified'}\n"
                    f"Run: {run.id}\nIncident: {incident.id}"
                ),
                "incident_id": incident.id,
            },
            rationale="an escalated incident needs a tracked, human-owned record",
            expected_state=expected_state_for("jira.create_incident", {}, settings=self.settings),
            sequence=98,
        )
        action, decision, notes = await self.executor.prepare(
            request, actor=actor, incident=incident, run_id=run.id
        )
        if action is None:
            await self.audit.append(
                AuditEventType.ACTION_PROPOSAL_REJECTED,
                actor=actor.actor_id,
                role=actor.role.value,
                incident_id=incident.id,
                agent_run_id=run.id,
                outcome="rejected",
                reason=f"escalation issue was not permitted: {decision.reason}",
            )
            _ = notes
            return None
        outcome = await self.executor.execute(action, actor=actor, incident=incident)
        if outcome.executed and outcome.result and outcome.result.data.get("key"):
            await self.incidents.update(
                incident.id, {"jira_issue_key": str(outcome.result.data["key"])}
            )
        return action

    async def _approved_action(self, run_id: str) -> Any | None:
        actions = await self.actions.list_for_incident((await self.runs.get(run_id)).incident_id)
        for action in actions:
            if not action.requires_approval:
                continue
            approval = await self.approvals.get_for_action(action.id)
            if approval is None or approval.decision is not ApprovalDecision.APPROVED:
                continue
            if not await self.approvals.verify_binding(approval.id, action=action):
                await self.audit.append(
                    AuditEventType.APPROVAL_INVALIDATED,
                    actor="system",
                    incident_id=action.incident_id,
                    tool_name=action.tool_name,
                    action_id=action.id,
                    approval_id=approval.id,
                    outcome="invalidated",
                    reason="approval no longer binds this payload (expired or changed)",
                )
                continue
            return action
        return None

    async def _execute(
        self, run: Any, incident: Incident, *, actor: Actor
    ) -> tuple[Any, str, Any | None]:
        actions = await self.actions.list_for_incident(incident.id)
        pending = [
            item
            for item in actions
            if item.status
            in {ActionStatus.PROPOSED, ActionStatus.AWAITING_APPROVAL, ActionStatus.EXECUTING}
        ]
        if not pending:
            return run, "no executable action remained in the plan", None

        executed_any: Any | None = None
        notes: list[str] = []
        for action in pending:
            outcome = await self.executor.execute(action, actor=actor, incident=incident)
            if outcome.awaiting_approval:
                notes.append(f"{action.tool_name}: awaiting approval")
                continue
            if outcome.executed:
                executed_any = action
                notes.append(f"{action.tool_name}: executed")
                await self._record_external_ids(incident, action, outcome)
            else:
                notes.append(f"{action.tool_name}: {outcome.reason}")
                if action.is_high_risk:
                    # A failed high-risk action is not retried silently; a human decides.
                    await self._escalate(
                        run,
                        incident,
                        actor=actor,
                        reason=f"{action.tool_name} failed: {outcome.reason}",
                    )

        if executed_any is None:
            return run, "; ".join(notes) or "no action executed", None
        return run, "; ".join(notes), executed_any

    async def _record_external_ids(self, incident: Incident, action: Any, outcome: Any) -> None:
        data = outcome.result.data if outcome.result else {}
        if action.tool_name == "jira.create_incident" and data.get("key"):
            await self.incidents.update(incident.id, {"jira_issue_key": str(data["key"])})
        if action.tool_name == "slack.notify" and data.get("message_ts"):
            await self.incidents.update(incident.id, {"slack_message_ts": str(data["message_ts"])})

    async def _verify(self, run: Any, incident: Incident, *, actor: Actor) -> tuple[Any, str]:
        action_id = None
        actions = await self.actions.list_for_incident(incident.id)
        # Verify the most *consequential* executed action: the highest risk one, most recent
        # first. Verifying only the last call would mean a successful notification could mask a
        # rollback that did not take effect. "Executed" is not "effective" — that is the point.
        executed = sorted(
            (item for item in actions if item.status is ActionStatus.SUCCEEDED),
            key=lambda item: (item.risk.rank, item.created_at),
            reverse=True,
        )
        if executed:
            action_id = executed[0].id

        if action_id is None:
            result = await self.verifier.verify_incident(incident.id, actor=actor.actor_id)
        else:
            # The expectation is read from the action row itself, where it was recorded
            # *before* execution — not reconstructed from the plan after the fact.
            result = await self.verifier.verify_action(action_id, actor=actor.actor_id)

        await self.incidents.update(
            incident.id,
            {
                "verification_outcome": result.outcome.value,
                "status": (
                    IncidentStatus.RESOLVED.value
                    if result.outcome is VerificationOutcome.SUCCESS
                    else IncidentStatus.ESCALATED.value
                ),
                "resolved_at": utcnow() if result.outcome is VerificationOutcome.SUCCESS else None,
                "escalation_reason": (
                    None
                    if result.outcome is VerificationOutcome.SUCCESS
                    else f"verification {result.outcome.value.lower()}: {result.reason}"
                ),
            },
        )

        if result.outcome is VerificationOutcome.SUCCESS:
            run, _ = await self.runs.transition(
                run.id,
                AgentState.RESOLVED,
                actor=actor.actor_id,
                reason=f"verified: {result.reason}"[:500],
                expected_version=run.version,
                extra={"stage": "resolved"},
            )
            await self.audit.append(
                AuditEventType.INCIDENT_RESOLVED,
                actor=actor.actor_id,
                role=actor.role.value,
                incident_id=incident.id,
                agent_run_id=run.id,
                outcome="resolved",
                reason=result.reason[:500],
                payload={"verification_id": result.id, "checks": len(result.checks)},
            )
            return run, f"resolved: {result.reason}"

        run = await self._escalate(
            run,
            incident,
            actor=actor,
            reason=f"verification {result.outcome.value.lower()}: {result.reason}",
            verification_id=result.id,
        )
        await self._ensure_issue(run, incident, actor=actor)
        return run, f"escalated: {result.reason}"

    async def _escalate(
        self,
        run: Any,
        incident: Incident,
        *,
        actor: Actor,
        reason: str,
        verification_id: str | None = None,
    ) -> Any:
        if run.state is AgentState.ESCALATED:
            return run
        from app.agent.state_machine import is_allowed

        if not is_allowed(run.state, AgentState.ESCALATED):
            logger.warning("escalation_not_allowed", state=run.state.value)
            return run
        await record_transition_audit(
            self.audit,
            from_state=run.state,
            to_state=AgentState.ESCALATED,
            run_id=run.id,
            incident_id=incident.id,
            actor=actor.actor_id,
            role=actor.role.value,
            reason=reason[:400],
        )
        run, _ = await self.runs.transition(
            run.id,
            AgentState.ESCALATED,
            actor=actor.actor_id,
            reason=reason[:500],
            expected_version=run.version,
            extra={"stage": "escalated", "failure_reason": reason[:500]},
        )
        await self.incidents.update(
            incident.id,
            {"status": IncidentStatus.ESCALATED.value, "escalation_reason": reason[:500]},
        )
        ESCALATIONS.labels(reason=_escalation_reason(reason)).inc()
        await self.audit.append(
            AuditEventType.INCIDENT_ESCALATED,
            actor=actor.actor_id,
            role=actor.role.value,
            incident_id=incident.id,
            agent_run_id=run.id,
            outcome="escalated",
            reason=reason[:500],
            payload={"verification_id": verification_id},
        )
        return run

    async def _transition(
        self,
        run: Any,
        to_state: AgentState,
        *,
        actor: Actor,
        reason: str,
        guard: dict[str, Any] | None = None,
    ) -> Any:
        assert_transition(run.state, to_state, guard=guard)
        await record_transition_audit(
            self.audit,
            from_state=run.state,
            to_state=to_state,
            run_id=run.id,
            incident_id=run.incident_id,
            actor=actor.actor_id,
            role=actor.role.value,
            reason=reason,
        )
        updated, _ = await self.runs.transition(
            run.id,
            to_state,
            actor=actor.actor_id,
            reason=reason,
            expected_version=run.version,
            extra={"stage": to_state.value.lower()},
        )
        return updated

    # ------------------------------------------------------------------ #
    # Snapshot
    # ------------------------------------------------------------------ #

    async def _snapshot(self, run: Any, *, stage_note: str = "") -> dict[str, Any]:
        transitions = await self.runs.list_transitions(run.id)
        actions = await self.actions.list_for_incident(run.incident_id)
        approval_summary = []
        pending_approval_id = None
        for action in actions:
            if not action.requires_approval:
                continue
            approval = await self.approvals.get_for_action(action.id)
            if approval is None:
                continue
            approval_summary.append(
                {
                    "approval_id": approval.id,
                    "action_id": action.id,
                    "tool_name": action.tool_name,
                    "risk": action.risk.value,
                    "decision": approval.decision.value,
                    "expires_at": approval.expires_at.isoformat(),
                    "payload_hash": approval.payload_hash[:32],
                }
            )
            if approval.decision is ApprovalDecision.PENDING:
                pending_approval_id = pending_approval_id or approval.id

        verification = await self.verifications.latest_for_incident(run.incident_id)
        return {
            "run": {
                "id": run.id,
                "incident_id": run.incident_id,
                "state": run.state.value,
                "stage": run.stage,
                "version": run.version,
                "reasoner": run.reasoner,
                "degraded_reason": run.degraded_reason,
                "simulated": run.simulated,
                "started_at": run.started_at.isoformat(),
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            },
            "transitions": [
                {
                    "from_state": item.from_state.value,
                    "to_state": item.to_state.value,
                    "actor": item.actor,
                    "reason": item.reason,
                    "occurred_at": item.occurred_at.isoformat(),
                }
                for item in transitions
            ],
            "diagnosis": run.diagnosis,
            "plan": run.plan,
            "actions": [
                {
                    "id": item.id,
                    "tool_name": item.tool_name,
                    "risk": item.risk.value,
                    "status": item.status.value,
                    "policy_decision": item.policy_decision,
                    "policy_reason": item.policy_reason,
                    "requires_approval": item.requires_approval,
                    "result": item.result,
                    "error": item.error,
                }
                for item in actions
            ],
            "approvals": approval_summary,
            "verification": (
                {
                    "id": verification.id,
                    "outcome": verification.outcome.value,
                    "reason": verification.reason,
                    "checks": verification.checks,
                    "completed_at": verification.completed_at.isoformat(),
                }
                if verification
                else None
            ),
            "requires_human": run.state is AgentState.WAITING_APPROVAL,
            "pending_approval_id": pending_approval_id,
            "note": stage_note,
        }


def _escalation_reason(reason: str) -> str:
    lowered = reason.lower()
    for key in (
        "verification failed",
        "verification unknown",
        "rollback",
        "timeout",
        "integration",
        "llm",
        "policy",
    ):
        if key in lowered:
            return key.replace(" ", "_")
    return "other"


__all__ = ["NOTIFY_FROM_SEVERITY", "IncidentOrchestrator"]
