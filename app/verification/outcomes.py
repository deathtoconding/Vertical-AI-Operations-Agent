"""Aggregation and reporting of verification outcomes (OPS-063).

The rule set is deliberately pessimistic and is quoted in one place so it can be argued with:

```
no checks            -> UNKNOWN   (we did not observe anything, so we do not know)
any FAILED           -> FAILED    (a contradiction outranks an inconclusive check)
any UNKNOWN          -> UNKNOWN   (some evidence is missing, so the answer is "unclear")
otherwise            -> SUCCESS ```

Rounding ``UNKNOWN`` up to success is the single most damaging shortcut available to a system
like this: it converts "we could not tell" into "it worked", and it does so exactly when the
integration is broken.
"""

from __future__ import annotations

from app.domain.enums import VerificationOutcome
from app.domain.verification import CheckResult, VerificationResult, aggregate_outcomes

#: Plain-language guidance attached to each outcome, surfacing in API responses.
GUIDANCE: dict[VerificationOutcome, str] = {
    VerificationOutcome.SUCCESS: (
        "the observed state matches the expectation declared before execution"
    ),
    VerificationOutcome.FAILED: (
        "the observed state contradicts the expectation; a human must review"
    ),
    VerificationOutcome.UNKNOWN: (
        "the evidence was insufficient; treat this as unresolved, not as success"
    ),
}


def outcome_guidance(outcome: VerificationOutcome) -> str:
    return GUIDANCE.get(outcome, "no guidance available")


def summarise(results: list[CheckResult], outcome: VerificationOutcome) -> str:
    """Human-readable summary that never hides a failure behind a success count."""
    if not results:
        return "no checks were run, so the outcome is unknown"
    failed = [check for check in results if check.outcome is VerificationOutcome.FAILED]
    unknown = [check for check in results if check.outcome is VerificationOutcome.UNKNOWN]
    succeeded = [check for check in results if check.outcome is VerificationOutcome.SUCCESS]
    parts = [
        f"{outcome.value.lower()}: {len(succeeded)} passed, {len(failed)} failed, "
        f"{len(unknown)} inconclusive"
    ]
    if failed:
        parts.append(
            "failures: " + "; ".join(f"{check.check} ({check.reason})" for check in failed)
        )
    if unknown:
        parts.append(
            "inconclusive: " + "; ".join(f"{check.check} ({check.reason})" for check in unknown)
        )
    return " | ".join(parts)


def resolves_incident(result: VerificationResult) -> bool:
    """Only a SUCCESS resolves an incident; everything else needs a human."""
    return result.outcome is VerificationOutcome.SUCCESS


__all__ = [
    "GUIDANCE",
    "aggregate_outcomes",
    "outcome_guidance",
    "resolves_incident",
    "summarise",
]
