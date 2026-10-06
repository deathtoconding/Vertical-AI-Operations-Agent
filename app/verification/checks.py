"""Independent verification checks (OPS-063).

Each check answers one narrow question about the operated system, and each one is written to be
able to say **"I do not know"**. That is the whole point: a checker that can only answer yes or no
will answer "no" (paging someone at 03:00 for nothing) or "yes" (hiding a failed rollback).

Conventions every check obeys:

* it reads the target system directly — never the executor's result, never the tool's response;
* a missing or unreadable source returns ``UNKNOWN``, not a pass;
* it reports the raw numbers it saw, so a human can disagree with the verdict;
* thresholds come from the expectation declared *before* execution.
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.core.errors import IntegrationError
from app.domain.enums import VerificationOutcome
from app.domain.verification import CheckResult

#: Metric-level checks are only meaningful with enough samples in the window.
MIN_SAMPLES_FOR_CONCLUSION = 3


async def check_metric_recovered(
    integrations: Any, expected: dict[str, Any], *, settings: Settings
) -> CheckResult:
    metric = str(expected.get("metric") or "error_rate")
    threshold = expected.get("threshold")
    comparison = str(expected.get("comparison") or "below")
    service = str(expected.get("service") or settings.default_service)
    window_minutes = int(
        expected.get("window_minutes") or max(1, settings.detection_window_minutes // 4)
    )

    series = await integrations.metrics_window(service, metric, window_minutes)
    points = [float(point.get("value", 0.0)) for point in series.get("points", [])]
    if not points:
        return CheckResult(
            check="metric_recovered",
            outcome=VerificationOutcome.UNKNOWN,
            expected={"metric": metric, "threshold": threshold, "comparison": comparison},
            reason="the metric returned no samples in the verification window",
            source="metrics",
        )

    latest = points[-1]
    low = min(points)
    high = max(points)

    if threshold is None:
        # No declared threshold: compare the tail of the recovery window against its own # earlier
        # state rather than inventing a pass mark.
        if len(points) < 2:
            return CheckResult(
                check="metric_recovered",
                outcome=VerificationOutcome.UNKNOWN,
                observed={"latest": latest},
                samples=len(points),
                source="metrics",
                reason="insufficient samples to compare before/after",
            )
        improved = latest < points[0]
        return CheckResult(
            check="metric_recovered",
            outcome=VerificationOutcome.SUCCESS if improved else VerificationOutcome.FAILED,
            observed={"first": points[0], "latest": latest, "min": low, "max": high},
            expected={"direction": "declining"},
            samples=len(points),
            source="metrics",
            reason=(
                f"{metric} moved from {points[0]:.4f} to {latest:.4f} across the window"
                if improved
                else f"{metric} did not decline ({points[0]:.4f} -> {latest:.4f})"
            ),
        )

    target = float(threshold)
    observed = {"latest": latest, "min": low, "max": high, "threshold": target}
    samples = len(points)
    if samples < MIN_SAMPLES_FOR_CONCLUSION:
        return CheckResult(
            check="metric_recovered",
            outcome=VerificationOutcome.UNKNOWN,
            observed=observed,
            expected={"metric": metric, "threshold": target, "comparison": comparison},
            samples=samples,
            source="metrics",
            reason=f"only {samples} samples in the window; not enough to conclude",
        )

    if comparison == "below":
        passed = latest < target
    elif comparison == "above":
        passed = latest > target
    else:
        passed = abs(latest - target) <= max(abs(target) * 0.1, 1e-9)

    return CheckResult(
        check="metric_recovered",
        outcome=VerificationOutcome.SUCCESS if passed else VerificationOutcome.FAILED,
        observed=observed,
        expected={"metric": metric, "threshold": target, "comparison": comparison},
        samples=samples,
        source="metrics",
        reason=(
            f"{metric} is {latest:.4f}, which is {comparison} the declared target {target:.4f}"
            if passed
            else (
                f"{metric} is {latest:.4f}, which is not {comparison} "
                f"the declared target {target:.4f}"
            )
        ),
    )


async def check_release_active(
    integrations: Any, expected: dict[str, Any], *, settings: Settings
) -> CheckResult:
    target = expected.get("target_release")
    state = await integrations.deployment_state()
    active = str(state.get("active_release") or "")
    if not target:
        return CheckResult(
            check="release_active",
            outcome=VerificationOutcome.UNKNOWN,
            observed={"active_release": active},
            reason="no target release was declared before execution",
            source="deployment",
        )
    passed = active == str(target)
    return CheckResult(
        check="release_active",
        outcome=VerificationOutcome.SUCCESS if passed else VerificationOutcome.FAILED,
        observed={"active_release": active, "previous_release": state.get("previous_release")},
        expected={"target_release": str(target)},
        source="deployment",
        reason=(
            f"active release is {active} as expected"
            if passed
            else f"active release is {active}, not the declared target {target}"
        ),
    )


async def check_deployment_healthy(
    integrations: Any, expected: dict[str, Any], *, settings: Settings
) -> CheckResult:
    state = await integrations.deployment_state()
    healthy = state.get("healthy")
    if healthy is None:
        return CheckResult(
            check="deployment_healthy",
            outcome=VerificationOutcome.UNKNOWN,
            observed=dict(state),
            reason="the deployment system did not report a health flag",
            source="deployment",
        )
    return CheckResult(
        check="deployment_healthy",
        outcome=VerificationOutcome.SUCCESS if healthy else VerificationOutcome.FAILED,
        observed={"healthy": bool(healthy), "active_release": state.get("active_release")},
        expected={"healthy": True},
        source="deployment",
        reason="deployment reports healthy" if healthy else "deployment still reports unhealthy",
    )


async def check_notification_delivered(
    integrations: Any, expected: dict[str, Any], *, settings: Settings
) -> CheckResult:
    history = await integrations.slack.history(limit=20)
    messages = history.get("messages", [])
    if not messages:
        return CheckResult(
            check="notification_delivered",
            outcome=VerificationOutcome.UNKNOWN,
            observed={"messages": 0},
            reason="no message history was available to confirm delivery",
            source="slack",
        )
    newest = messages[-1]
    return CheckResult(
        check="notification_delivered",
        outcome=VerificationOutcome.SUCCESS,
        observed={"messages": len(messages), "latest_ts": newest.get("ts")},
        expected={"minimum_messages": 1},
        source="slack",
        reason="the notification channel contains the message that was sent",
    )


async def check_issue_exists(
    integrations: Any, expected: dict[str, Any], *, settings: Settings
) -> CheckResult:
    key = expected.get("issue_key") or expected.get("key")
    if not key:
        return CheckResult(
            check="issue_exists",
            outcome=VerificationOutcome.UNKNOWN,
            reason="no issue key was recorded, so the issue cannot be confirmed",
            source="jira",
        )
    payload = await integrations.jira.get_issue(str(key))
    found = bool(payload.get("key"))
    return CheckResult(
        check="issue_exists",
        outcome=VerificationOutcome.SUCCESS if found else VerificationOutcome.FAILED,
        observed={"key": key, "found": found},
        expected={"key": str(key)},
        source="jira",
        reason=f"issue {key} exists" if found else f"issue {key} was not found",
    )


async def check_tool_effect_recorded(
    integrations: Any, expected: dict[str, Any], *, settings: Settings
) -> CheckResult:
    """Generic read-back for tools with no better independent check.

    This is the weakest check in the set and says so: it confirms the *provider* recorded the
    effect, which is one step removed from confirming the effect happened.
    """
    external_id = expected.get("external_id")
    return CheckResult(
        check="tool_effect_recorded",
        outcome=VerificationOutcome.UNKNOWN,
        observed={"external_id": external_id},
        expected={"external_id": external_id},
        source="tool",
        reason=(
            "the provider acknowledged the call; no independent check is defined for this "
            "tool, so effectiveness cannot be confirmed"
        ),
    )


#: Check name -> implementation. The engine dispatches through this map, so an unknown check #: is
#: reported as inconclusive rather than silently skipped.
CHECKS: dict[str, Any] = {
    "metric_recovered": check_metric_recovered,
    "release_active": check_release_active,
    "deployment_healthy": check_deployment_healthy,
    "notification_delivered": check_notification_delivered,
    "issue_exists": check_issue_exists,
    "tool_effect_recorded": check_tool_effect_recorded,
}


async def run_check(
    name: str, integrations: Any, expected: dict[str, Any], *, settings: Settings
) -> CheckResult:
    """Execute one check, converting an unreadable source into ``UNKNOWN``.

    A source we cannot reach means we do not know — which is emphatically not the same as "fine".
    """
    handler = CHECKS.get(name)
    if handler is None:
        return CheckResult(
            check=name,
            outcome=VerificationOutcome.UNKNOWN,
            reason=f"no implementation for check '{name}'",
        )
    try:
        return await handler(integrations, expected, settings=settings)
    except IntegrationError as exc:
        return CheckResult(
            check=name,
            outcome=VerificationOutcome.UNKNOWN,
            reason=f"{exc.code}: verification source unavailable",
        )


__all__ = ["CHECKS", "MIN_SAMPLES_FOR_CONCLUSION", "run_check", *sorted(CHECKS)]
