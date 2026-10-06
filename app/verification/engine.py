"""The verification engine (OPS-063 / ADR-0004).

**A tool returning HTTP 200 is not evidence that anything improved.** The verifier therefore
never reads the executor's result as proof. It goes back to the systems and asks a different
question: *is the world now consistent with the expectation that was declared before the
action ran?*

Design consequences that follow from that sentence:

* expectations are declared **before** execution (``Action.expected_state``), never derived
  afterwards from whatever happened;
* each check reads the operated system directly through the integration facade, not through
  the tool that performed the action;
* sampling is a *window*, not a single observation, because recovery is not instantaneous —
  a verifier that samples once, immediately, would report failure for a successful rollback
  and success for a slow-burn regression;
* aggregation is pessimistic (:func:`~app.domain.verification.aggregate_outcomes`):
  empty -> UNKNOWN, any FAILED -> FAILED, else any UNKNOWN -> UNKNOWN, else SUCCESS;
* ``UNKNOWN`` is a real answer and is escalated to a human rather than rounded to success.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.telemetry import VERIFICATION_LATENCY, VERIFICATION_RESULTS
from app.domain.enums import AuditEventType
from app.domain.verification import CheckResult, VerificationResult
from app.integrations.facade import IntegrationFacade
from app.persistence.models.base import new_prefixed_id
from app.persistence.repositories.actions import ActionRepository, VerificationRepository
from app.persistence.repositories.audit import AuditRepository
from app.verification.checks import MIN_SAMPLES_FOR_CONCLUSION, run_check
from app.verification.outcomes import aggregate_outcomes, summarise

logger = get_logger(__name__)


class VerificationEngine:
    """Independently check whether an executed action had the intended effect."""

    def __init__(
        self,
        settings: Settings,
        integrations: IntegrationFacade,
        *,
        verifications: VerificationRepository,
        actions: ActionRepository,
        audit: AuditRepository,
    ) -> None:
        self.settings = settings
        self.integrations = integrations
        self.verifications = verifications
        self.actions = actions
        self.audit = audit

    # ------------------------------------------------------------------ #
    # Public entry points
    # ------------------------------------------------------------------ #

    async def verify_action(
        self,
        action_id: str,
        *,
        expected_state: dict[str, Any] | None = None,
        actor: str = "system",
        wait_seconds: float | None = None,
    ) -> VerificationResult:
        action = await self.actions.get(action_id)
        if action is None:
            raise ValueError(f"action {action_id} does not exist")

        expected = expected_state or action.expected_state or {}
        window = float(
            wait_seconds if wait_seconds is not None else self.settings.verification_window_seconds
        )
        return await self._run(
            incident_id=action.incident_id,
            action_id=action.id,
            run_id=action.run_id,
            checks=self._plan_checks(action.tool_name, expected),
            expected=expected,
            window_seconds=window,
            actor=actor,
        )

    async def verify_incident(
        self,
        incident_id: str,
        *,
        expected_state: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> VerificationResult:
        """Incident-level verification (used when there is no single action to attribute)."""
        expected = expected_state or {
            "checks": [
                {"check": "metric_recovered", "metric": "error_rate"},
                {"check": "deployment_healthy"},
            ]
        }
        return await self._run(
            incident_id=incident_id,
            action_id=None,
            run_id=None,
            checks=self._plan_checks(None, expected),
            expected=expected,
            window_seconds=self.settings.verification_window_seconds,
            actor=actor,
        )

    # ------------------------------------------------------------------ #
    # Check planning
    # ------------------------------------------------------------------ #

    def _plan_checks(self, tool_name: str | None, expected: dict[str, Any]) -> list[str]:
        explicit = [
            str(check.get("check"))
            for check in (expected.get("checks") or [])
            if isinstance(check, dict) and check.get("check")
        ]
        if explicit:
            return explicit
        if expected.get("metric"):
            checks = ["metric_recovered"]
            if expected.get("target_release"):
                checks.append("release_active")
            return checks
        if tool_name == "deployment.rollback_simulation":
            return ["release_active", "deployment_healthy", "metric_recovered"]
        if tool_name == "slack.notify":
            return ["notification_delivered"]
        if tool_name == "jira.create_incident":
            return ["issue_exists"]
        if tool_name:
            return ["tool_effect_recorded"]
        return ["deployment_healthy", "metric_recovered"]

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #

    async def _run(
        self,
        *,
        incident_id: str,
        action_id: str | None,
        run_id: str | None,
        checks: list[str],
        expected: dict[str, Any],
        window_seconds: float,
        actor: str,
    ) -> VerificationResult:
        started_at = datetime.now(UTC)
        started = perf_counter()
        await self.audit.append(
            AuditEventType.VERIFICATION_STARTED,
            actor=actor,
            incident_id=incident_id,
            agent_run_id=run_id,
            action_id=action_id,
            outcome="started",
            payload={"checks": checks, "window_seconds": window_seconds},
        )

        # Let the world settle before observing: an instantaneous check is a coin flip.
        if window_seconds > 0:
            await self._settle(window_seconds)

        results: list[CheckResult] = []
        for name in checks:
            results.append(await self._run_check(name, expected))

        outcome = aggregate_outcomes(results)
        reason = summarise(results, outcome)
        completed_at = datetime.now(UTC)

        record = await self.verifications.create(
            {
                "id": new_prefixed_id("VER"),
                "incident_id": incident_id,
                "action_id": action_id,
                "run_id": run_id,
                "outcome": outcome.value,
                "reason": reason,
                "checks": [check.model_dump(mode="json") for check in results],
                "observed": {
                    check.check: {"observed": check.observed, "expected": check.expected}
                    for check in results
                },
                "started_at": started_at,
                "completed_at": completed_at,
                "duration_seconds": round(perf_counter() - started, 4),
            }
        )

        VERIFICATION_RESULTS.labels(outcome=outcome.value, check="aggregate").inc()
        VERIFICATION_LATENCY.observe(record.duration_seconds)
        await self.audit.append(
            AuditEventType.VERIFICATION_COMPLETED,
            actor=actor,
            incident_id=incident_id,
            agent_run_id=run_id,
            action_id=action_id,
            outcome=outcome.value,
            reason=reason,
            payload={
                "checks": [
                    {"check": check.check, "outcome": check.outcome.value, "reason": check.reason}
                    for check in results
                ],
                "duration_seconds": record.duration_seconds,
            },
        )
        logger.info(
            "verification_completed",
            incident_id=incident_id,
            action_id=action_id,
            outcome=outcome.value,
            checks=len(results),
        )
        return record

    async def _settle(self, window_seconds: float) -> None:
        interval = max(0.0, self.settings.verification_poll_interval_seconds)
        remaining = window_seconds
        while remaining > 0:
            step = min(interval or remaining, remaining)
            await asyncio.sleep(step)
            remaining -= step

    # ------------------------------------------------------------------ #
    # Individual checks
    # ------------------------------------------------------------------ #

    async def _run_check(self, name: str, expected: dict[str, Any]) -> CheckResult:
        """Delegate to :mod:`app.verification.checks`; unknown checks are inconclusive."""
        return await run_check(name, self.integrations, expected, settings=self.settings)


__all__ = ["MIN_SAMPLES_FOR_CONCLUSION", "VerificationEngine"]
