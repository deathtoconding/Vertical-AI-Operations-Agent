"""Action execution: the only path from a proposal to a side effect (OPS-020..OPS-023).

The executor is a sequence of gates. Each gate can stop execution, and stopping is a
first-class, recorded outcome rather than an exception nobody sees:

```
proposal
  -> tool lookup        (unregistered => DENY, counted, audited, never executed)
  -> policy gate        (DENY => rejected; REQUIRE_APPROVAL => park as AWAITING_APPROVAL)
  -> approval gate      (bound to the exact payload hash, unexpired, decided by a human)
  -> idempotency gate   (replay returns the first result instead of repeating the effect)
  -> invoke             (timeout, bounded retries for *retryable* outcomes only)
  -> record             (ToolInvocation + Action status + audit event)
```

Two deliberate properties:

* **Execution is not verification.** ``ToolResult.success`` means the call was made and
  answered. Whether the incident actually improved is decided by
  :mod:`app.verification`, which reads the world independently (ADR-0004).
* **Failure is not silent.** A tool that fails yields ``ActionStatus.FAILED`` with the typed
  reason, which the orchestrator escalates to a human rather than retrying indefinitely.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from app.core.config import Settings
from app.core.errors import (
    AppError,
    PolicyDenied,
    ToolNotFound,
    ValidationFailed,
)
from app.core.logging import get_logger
from app.core.security import Actor
from app.core.telemetry import (
    ACTION_FAILURES,
    ACTION_PROPOSALS_REJECTED,
    TOOL_INVOCATIONS,
    TOOL_LATENCY,
    UNSAFE_ACTION_ATTEMPTS,
)
from app.core.tracing import span
from app.domain.actions import (
    Action,
    ActionRequest,
    action_payload,
    canonical_hash,
    derive_idempotency_key,
)
from app.domain.enums import (
    ActionStatus,
    AuditEventType,
    PolicyDecisionType,
    ToolOutcome,
)
from app.domain.incidents import Incident
from app.domain.policy import PolicyDecision
from app.domain.tools import ToolResult
from app.persistence.models.base import new_prefixed_id, utcnow
from app.persistence.repositories.actions import ActionRepository, ToolInvocationRepository
from app.persistence.repositories.audit import AuditRepository
from app.persistence.repositories.idempotency import IdempotencyStore
from app.policy.engine import PolicyEngine
from app.tools.registry import ToolContext, ToolRegistry

logger = get_logger(__name__)

IDEMPOTENCY_SCOPE = "action"


@dataclass
class ExecutionOutcome:
    """What happened to one action attempt."""

    action: Action
    executed: bool = False
    result: ToolResult | None = None
    policy: PolicyDecision | None = None
    replayed: bool = False
    #: True when a human must decide before this action can run.
    awaiting_approval: bool = False
    reason: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Executed *and* the call answered successfully — still not "verified effective"."""
        return bool(self.executed and self.result is not None and self.result.success)


class ActionExecutor:
    """Execute (or refuse) one action, recording every step."""

    def __init__(
        self,
        settings: Settings,
        registry: ToolRegistry,
        policy: PolicyEngine,
        *,
        actions: ActionRepository,
        approvals: Any,  # ApprovalRepository — kept loose to avoid an import cycle
        invocations: ToolInvocationRepository,
        audit: AuditRepository,
        idempotency: IdempotencyStore,
        integrations: Any,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.policy = policy
        self.actions = actions
        self.approvals = approvals
        self.invocations = invocations
        self.audit = audit
        self.idempotency = idempotency
        self.integrations = integrations
        self.retry_backoff_seconds = settings.action_retry_backoff_seconds

    # ------------------------------------------------------------------ #
    # Planning-time helpers
    # ------------------------------------------------------------------ #

    async def prepare(
        self,
        request: ActionRequest,
        *,
        actor: Actor,
        incident: Incident | None = None,
        run_id: str | None = None,
        prior_rollbacks: int = 0,
    ) -> tuple[Action | None, PolicyDecision, list[str]]:
        """Validate a proposal against policy *before* persisting it.

        Returns ``(action_or_None, decision, notes)``. A ``None`` action means the proposal
        was refused at planning time — the incident continues without it, and the refusal is
        recorded with the rule that caused it.
        """
        notes: list[str] = []
        try:
            definition = self.registry.get(request.tool_name)
        except ToolNotFound as exc:
            UNSAFE_ACTION_ATTEMPTS.labels(tool=request.tool_name[:64], outcome="blocked").inc()
            ACTION_PROPOSALS_REJECTED.labels(reason="unknown_tool").inc()
            decision = self.policy.evaluate(
                request.tool_name, actor=actor, params=request.params, incident=incident
            )
            await self.audit.append(
                AuditEventType.UNKNOWN_TOOL_REQUESTED,
                actor=actor.actor_id,
                role=actor.role.value,
                incident_id=request.incident_id,
                agent_run_id=run_id,
                tool_name=request.tool_name[:120],
                outcome="denied",
                reason="unknown_tool",
                payload={"detail": str(exc)[:200]},
            )
            notes.append(f"tool '{request.tool_name}' is not registered and cannot be executed")
            return None, decision, notes

        # Deterministic parameter completion: a tool that declares ``incident_id`` is always
        # scoped to the incident under investigation. The model is not asked for it (and must
        # not be able to point it elsewhere — ``rule_incident_scope`` denies a mismatch).
        params = dict(request.params)
        if "incident_id" in definition.params_model.model_fields and not params.get("incident_id"):
            params["incident_id"] = request.incident_id
            notes.append("incident_id was filled from the incident under investigation")

        # A hint only: policy is the authority. The note makes the eventual denial easier to
        # read, but the executor does not act on this check.
        if not any(
            permission.value == definition.spec.permission for permission in actor.permissions()
        ):
            notes.append("actor lacks the tool's permission; policy will deny it")

        decision = self.policy.evaluate(
            request.tool_name,
            actor=actor,
            params=params,
            incident=incident,
            prior_rollbacks=prior_rollbacks,
        )

        await self.audit.append(
            AuditEventType.POLICY_EVALUATED,
            actor=actor.actor_id,
            role=actor.role.value,
            incident_id=request.incident_id,
            agent_run_id=run_id,
            tool_name=request.tool_name,
            outcome=decision.decision.value,
            reason=decision.reason,
            payload={
                "risk": decision.risk.value,
                "rule_violations": decision.violations,
                "requires_approval": decision.requires_approval,
            },
        )

        if decision.decision is PolicyDecisionType.DENY:
            if definition.spec.is_high_risk or decision.violations:
                UNSAFE_ACTION_ATTEMPTS.labels(tool=request.tool_name[:64], outcome="denied").inc()
            ACTION_PROPOSALS_REJECTED.labels(
                reason=(decision.violations[0] if decision.violations else "policy_denied")[:64]
            ).inc()
            await self.audit.append(
                AuditEventType.ACTION_PROPOSAL_REJECTED,
                actor=actor.actor_id,
                role=actor.role.value,
                incident_id=request.incident_id,
                agent_run_id=run_id,
                tool_name=request.tool_name,
                outcome="denied",
                reason=decision.reason,
                payload={
                    "params_hash": canonical_hash(action_payload(request.tool_name, params))[:32]
                },
            )
            notes.append(f"policy denied this action: {decision.reason}")
            return None, decision, notes

        status = (
            ActionStatus.AWAITING_APPROVAL
            if decision.decision is PolicyDecisionType.REQUIRE_APPROVAL
            else ActionStatus.PROPOSED
        )
        action = await self.actions.create(
            {
                "id": new_prefixed_id("ACT"),
                "incident_id": request.incident_id,
                "run_id": run_id,
                "tool_name": request.tool_name,
                "risk": decision.risk.value,
                "permission": definition.spec.permission,
                "params": params,
                "canonical_hash": canonical_hash(action_payload(request.tool_name, params)),
                "idempotency_key": derive_idempotency_key(
                    request.incident_id, request.tool_name, params
                ),
                "rationale": request.rationale,
                "evidence_ids": list(request.evidence_ids),
                "requires_approval": decision.decision is PolicyDecisionType.REQUIRE_APPROVAL,
                "status": status.value,
                "policy_decision": decision.decision.value,
                "policy_reason": decision.reason,
                "expected_state": request.expected_state.model_dump(mode="json"),
                "simulated": bool(definition.spec.simulated),
            }
        )
        await self.audit.append(
            AuditEventType.ACTION_PROPOSED,
            actor=actor.actor_id,
            role=actor.role.value,
            incident_id=request.incident_id,
            agent_run_id=run_id,
            tool_name=request.tool_name,
            action_id=action.id,
            # Audit outcome strings are bounded to 16 characters by the schema; keep this a
            # short, stable vocabulary rather than echoing a long enum name.
            outcome=("pending" if status is ActionStatus.AWAITING_APPROVAL else "proposed"),
            reason=decision.reason,
            payload={"risk": decision.risk.value, "requires_approval": action.requires_approval},
        )
        return action, decision, notes

    # ------------------------------------------------------------------ #
    # Execution-time gate
    # ------------------------------------------------------------------ #

    async def execute(
        self,
        action: Action,
        *,
        actor: Actor,
        incident: Incident | None = None,
        approval_id: str | None = None,
    ) -> ExecutionOutcome:
        # 0. Tamper check: the stored payload must still hash to the stored hash.
        if not action.payload_is_intact:
            await self._fail(
                action,
                actor,
                reason="action payload does not match its recorded hash",
                event=AuditEventType.VALIDATION_REJECTED,
            )
            return ExecutionOutcome(
                action=action, reason="payload hash mismatch", notes=["payload tampered or corrupt"]
            )

        # 1. Approval gate.
        if action.requires_approval:
            approval = await self._find_approval(action, approval_id)
            if approval is None:
                await self.actions.update(
                    action.id, {"status": ActionStatus.AWAITING_APPROVAL.value}
                )
                return ExecutionOutcome(
                    action=action,
                    awaiting_approval=True,
                    reason="a human approval bound to this exact payload is required",
                )
            if approval.decision.value == "REJECTED":
                await self.actions.update(action.id, {"status": ActionStatus.REJECTED.value})
                await self.audit.append(
                    AuditEventType.APPROVAL_REJECTED,
                    actor=actor.actor_id,
                    role=actor.role.value,
                    incident_id=action.incident_id,
                    tool_name=action.tool_name,
                    action_id=action.id,
                    approval_id=approval.id,
                    outcome="rejected",
                    reason=approval.reason or "approver rejected the action",
                )
                return ExecutionOutcome(action=action, reason="approval was rejected by a human")
            bound = await self.approvals.verify_binding(approval.id, action=action)
            if not bound:
                await self.audit.append(
                    AuditEventType.APPROVAL_INVALIDATED,
                    actor=actor.actor_id,
                    role=actor.role.value,
                    incident_id=action.incident_id,
                    tool_name=action.tool_name,
                    action_id=action.id,
                    approval_id=approval.id,
                    outcome="invalidated",
                    reason="approval expired or no longer matches the payload",
                )
                return ExecutionOutcome(
                    action=action,
                    reason="approval is expired or does not match this payload",
                )

        # 2. Idempotency gate.
        request_hash = canonical_hash({"action": action.payload})
        row, created = await self.idempotency.reserve(
            IDEMPOTENCY_SCOPE, action.idempotency_key, request_hash
        )
        if not created:
            stored = dict(row.response or {})
            if stored:
                await self.actions.update(
                    action.id,
                    {
                        "status": ActionStatus.SUCCEEDED.value,
                        "result": stored,
                        "executed_at": action.executed_at or utcnow(),
                    },
                )
                await self.audit.append(
                    AuditEventType.ACTION_REPLAYED,
                    actor=actor.actor_id,
                    role=actor.role.value,
                    incident_id=action.incident_id,
                    tool_name=action.tool_name,
                    action_id=action.id,
                    outcome="replayed",
                    reason="identical action was already executed; reused the recorded result",
                )
                return ExecutionOutcome(
                    action=action,
                    executed=True,
                    replayed=True,
                    result=ToolResult(
                        tool_name=action.tool_name,
                        success=True,
                        outcome=ToolOutcome.SUCCESS,
                        data=stored,
                        external_id=row.external_id,
                        replayed=True,
                        simulated=bool(stored.get("simulated", False)),
                    ),
                    reason="idempotent replay",
                )

        # 3. Invoke with timeout and bounded retries.
        definition = self.registry.get(action.tool_name)
        await self.actions.update(
            action.id, {"status": ActionStatus.EXECUTING.value, "attempts": action.attempts + 1}
        )
        context = ToolContext(
            settings=self.settings,
            integrations=self.integrations,
            actor=actor.actor_id,
            incident_id=action.incident_id,
            run_id=action.run_id,
            idempotency=self.idempotency,
        )

        started = perf_counter()
        result: ToolResult
        attempts = 0
        try:
            # The invocation is the one step a human most wants to see in the trace: which tool,
            # at what risk, and whether it really ran.
            with span(
                "tool.invoke",
                incident_id=action.incident_id,
                run_id=action.run_id,
                tool_name=action.tool_name,
                risk_level=action.risk.value,
            ) as current:
                result, attempts = await self._invoke_with_retries(
                    definition, action.params, context
                )
                current.set_attribute(
                    "tool.outcome",
                    "SIMULATED" if result.simulated else result.outcome.value,
                )
        except ValidationFailed as exc:
            await self._record_invocation(
                action, actor, ToolOutcome.INVALID_INPUT, started, error=str(exc)
            )
            await self._fail(
                action,
                actor,
                reason=f"invalid parameters: {exc}",
                event=AuditEventType.ACTION_FAILED,
            )
            return ExecutionOutcome(action=action, reason=f"invalid parameters: {exc}")
        except ToolNotFound as exc:
            await self._record_invocation(
                action, actor, ToolOutcome.UNKNOWN_TOOL, started, error=str(exc)
            )
            await self._fail(
                action,
                actor,
                reason="tool disappeared from the registry",
                event=AuditEventType.VALIDATION_REJECTED,
            )
            return ExecutionOutcome(action=action, reason="tool is no longer registered")
        except PolicyDenied as exc:
            await self._record_invocation(
                action, actor, ToolOutcome.DENIED, started, error=str(exc)
            )
            await self._fail(
                action, actor, reason=str(exc), event=AuditEventType.ACTION_PROPOSAL_REJECTED
            )
            return ExecutionOutcome(action=action, reason=str(exc))
        except AppError as exc:
            await self._record_invocation(
                action, actor, ToolOutcome.FAILURE, started, error=str(exc)
            )
            await self._fail(action, actor, reason=str(exc), event=AuditEventType.ACTION_FAILED)
            return ExecutionOutcome(action=action, reason=str(exc))

        duration_ms = (perf_counter() - started) * 1000
        outcome = ToolOutcome.SIMULATED if result.simulated else result.outcome
        await self._record_invocation(
            action,
            actor,
            outcome,
            started,
            request=action.params,
            response=result.data,
            error=result.error,
            external_id=result.external_id,
        )
        await self.idempotency.complete(
            IDEMPOTENCY_SCOPE,
            action.idempotency_key,
            external_id=result.external_id,
            response=result.data,
        )

        if result.success:
            await self.actions.update(
                action.id,
                {
                    "status": ActionStatus.SUCCEEDED.value,
                    "result": result.data,
                    "executed_at": utcnow(),
                    "error": None,
                },
            )
            await self.audit.append(
                AuditEventType.ACTION_EXECUTED,
                actor=actor.actor_id,
                role=actor.role.value,
                incident_id=action.incident_id,
                agent_run_id=action.run_id,
                tool_name=action.tool_name,
                action_id=action.id,
                outcome=outcome.value,
                reason="tool answered successfully; effectiveness is decided by verification",
                payload={
                    "duration_ms": round(duration_ms, 2),
                    "attempts": attempts,
                    "external_id": result.external_id,
                    "simulated": result.simulated,
                },
            )
            TOOL_INVOCATIONS.labels(
                tool=action.tool_name[:64], risk=action.risk.value, outcome=outcome.value
            ).inc()
            TOOL_LATENCY.labels(tool=action.tool_name[:64]).observe(duration_ms / 1000)
            return ExecutionOutcome(action=action, executed=True, result=result, reason="executed")

        await self.actions.update(
            action.id,
            {"status": ActionStatus.FAILED.value, "error": result.error or "tool reported failure"},
        )
        await self.audit.append(
            AuditEventType.ACTION_FAILED,
            actor=actor.actor_id,
            role=actor.role.value,
            incident_id=action.incident_id,
            agent_run_id=action.run_id,
            tool_name=action.tool_name,
            action_id=action.id,
            outcome=result.outcome.value,
            reason=(result.error or "tool reported failure")[:500],
            payload={"duration_ms": round(duration_ms, 2), "attempts": attempts},
        )
        TOOL_INVOCATIONS.labels(
            tool=action.tool_name[:64], risk=action.risk.value, outcome=result.outcome.value
        ).inc()
        ACTION_FAILURES.labels(tool=action.tool_name[:64], reason=result.outcome.value).inc()
        return ExecutionOutcome(
            action=action, result=result, reason=result.error or "tool reported failure"
        )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    async def _find_approval(self, action: Action, approval_id: str | None) -> Any | None:
        if approval_id:
            return await self.approvals.get(approval_id)
        return await self.approvals.get_for_action(action.id)

    async def _invoke_with_retries(
        self, definition: Any, params: dict[str, Any], context: ToolContext
    ) -> tuple[ToolResult, int]:
        """Retry only what is safe to retry: a tool that says ``retryable``.

        Retrying a non-idempotent side effect is how one rollback becomes two, so the tool's
        declared ``idempotent`` flag is respected and the executor never retries a success.
        """
        timeout = definition.spec.timeout_seconds or self.settings.action_timeout_seconds
        max_attempts = 1 + (definition.spec.max_retries if definition.spec.idempotent else 0)
        last: ToolResult | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                result = await asyncio.wait_for(definition.invoke(params, context), timeout=timeout)
            except TimeoutError:
                result = ToolResult(
                    tool_name=definition.spec.name,
                    success=False,
                    outcome=ToolOutcome.TIMEOUT,
                    error=f"tool exceeded its {timeout:.0f}s timeout",
                )
            if result.success or result.outcome not in {ToolOutcome.TIMEOUT, ToolOutcome.FAILURE}:
                return result, attempt
            last = result
            if attempt < max_attempts:
                await asyncio.sleep(self.retry_backoff_seconds * attempt)
        if last is not None:
            return last, max_attempts
        return (
            ToolResult(
                tool_name=definition.spec.name,
                success=False,
                outcome=ToolOutcome.FAILURE,
                error="tool could not be invoked",
            ),
            max_attempts,
        )

    async def _record_invocation(
        self,
        action: Action,
        actor: Actor,
        outcome: ToolOutcome,
        started: float,
        *,
        request: dict[str, Any] | None = None,
        response: dict[str, Any] | None = None,
        error: str | None = None,
        external_id: str | None = None,
    ) -> None:
        await self.invocations.record(
            {
                "id": new_prefixed_id("TIV"),
                "run_id": action.run_id,
                "incident_id": action.incident_id,
                "tool_name": action.tool_name,
                "risk": action.risk.value,
                "outcome": outcome.value,
                "duration_ms": round((perf_counter() - started) * 1000, 2),
                "request": request or {},
                "response": response or {},
                "error": (error or "")[:500] or None,
                "actor": actor.actor_id,
                "simulated": action.simulated,
            }
        )
        if external_id:
            logger.info("tool_external_id", tool=action.tool_name, external_id=external_id)

    async def _fail(
        self, action: Action, actor: Actor, *, reason: str, event: AuditEventType
    ) -> None:
        await self.actions.update(
            action.id, {"status": ActionStatus.FAILED.value, "error": reason[:500]}
        )
        await self.audit.append(
            event,
            actor=actor.actor_id,
            role=actor.role.value,
            incident_id=action.incident_id,
            agent_run_id=action.run_id,
            tool_name=action.tool_name,
            action_id=action.id,
            outcome="failed",
            reason=reason[:500],
        )
        TOOL_INVOCATIONS.labels(
            tool=action.tool_name[:64], risk=action.risk.value, outcome=ToolOutcome.DENIED.value
        ).inc()
        ACTION_FAILURES.labels(tool=action.tool_name[:64], reason="denied").inc()


__all__ = ["IDEMPOTENCY_SCOPE", "ActionExecutor", "ExecutionOutcome"]
