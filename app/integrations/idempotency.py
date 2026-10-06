"""Integration-level idempotency keys (OPS-021).

Why this module exists next to :mod:`app.actions.idempotency`: the executor derives keys for
*actions* (incident + tool + parameters), while an integration needs keys for *provider
operations* (create this incident's ticket, post this run's notification). They answer
different questions:

* the action key answers "have I already tried to run this tool for this incident?";
* the integration key answers "has this provider already seen this exact operation?".

The second key has to survive a retry that happens **after** the executor has moved on — a
process restart, a lost response, a Jira timeout — which is why it is derived from the
operation identity rather than from a request id. Jira's ``idempotencyKey`` parameter is the
consumer; the sandbox provider implements the same contract so the behaviour is testable
without a network.

Keys are deterministic, bounded and log-safe: ``<system>.<operation>:<subject>:<digest>``. The
digest covers the payload, so changing what would be created creates a new issue, and an
unchanged replay returns the original one.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from typing import Any, Final

from app.domain.actions import canonical_hash

#: Jira accepts up to 255 characters for ``idempotencyKey``; stay well inside it.
MAX_KEY_LENGTH: Final[int] = 180

#: Digest length — 16 hex characters is far more collision-resistant than this use needs.
DIGEST_LENGTH: Final[int] = 16

_SAFE_SUBJECT = re.compile(r"[^A-Za-z0-9._:-]+")


def _subject(value: str) -> str:
    """Normalise the subject so the key stays readable and safe for logs and URLs."""
    return _SAFE_SUBJECT.sub("-", value.strip())[:48] or "unknown"


def integration_idempotency_key(
    system: str,
    operation: str,
    *,
    subject: str,
    payload: dict[str, Any] | None = None,
    volatile: Iterable[str] = (),
) -> str:
    """Deterministic key for one provider operation.

    ``subject`` is the business identity (an incident id, a run id); ``payload`` is hashed so a
    changed payload is a different operation. ``volatile`` names payload fields that must *not*
    influence the key — per-attempt context such as ``run_id`` — so a retry from a later run
    still replays the original side effect instead of creating a second one.
    """
    stable = {key: value for key, value in (payload or {}).items() if key not in set(volatile)}
    digest = hashlib.sha256(
        canonical_hash({"system": system, "operation": operation, "payload": stable}).encode()
    ).hexdigest()[:DIGEST_LENGTH]
    key = f"{system}.{operation}:{_subject(subject)}:{digest}"
    return key[:MAX_KEY_LENGTH]


def jira_issue_key(incident_id: str, payload: dict[str, Any] | None = None) -> str:
    """Key for ``jira.create_incident`` — one issue per incident payload (OPS-021)."""
    return integration_idempotency_key(
        "jira",
        "create_issue",
        subject=incident_id,
        payload=payload,
        volatile=("run_id", "created_at", "timestamp"),
    )


def slack_notification_key(incident_id: str, payload: dict[str, Any] | None = None) -> str:
    """Key for ``slack.notify`` — one notification per incident payload (OPS-022)."""
    return integration_idempotency_key(
        "slack",
        "notify",
        subject=incident_id,
        payload=payload,
        volatile=("run_id", "created_at", "timestamp"),
    )


__all__ = [
    "DIGEST_LENGTH",
    "MAX_KEY_LENGTH",
    "integration_idempotency_key",
    "jira_issue_key",
    "slack_notification_key",
]
