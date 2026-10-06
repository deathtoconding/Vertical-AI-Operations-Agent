"""Idempotency for actions (OPS-062).

Two functions and one constant:

* :func:`idempotency_key` — derived from *(incident, tool, canonical params)*, so an identical
  intent replays and a changed intent re-executes. A random key per attempt would make every
  retry a fresh side effect; a key that ignores parameters would make a changed rollback target
  a replay of the old one.
* :func:`request_hash` — what the stored key is bound to, so a *different* payload under the
  same key is a detected conflict rather than a silent overwrite.
"""

from __future__ import annotations

from typing import Any

from app.domain.actions import Action, canonical_hash, derive_idempotency_key

#: Store scope for the action idempotency keys.
IDEMPOTENCY_SCOPE = "action"


def idempotency_key(incident_id: str, tool_name: str, params: dict[str, Any]) -> str:
    """Deterministic key for one intent."""
    return derive_idempotency_key(incident_id, tool_name, params)


def request_hash(action: Action) -> str:
    """Hash of the exact payload an idempotency record is bound to."""
    return canonical_hash({"action": {"tool_name": action.tool_name, "params": action.params}})


__all__ = ["IDEMPOTENCY_SCOPE", "idempotency_key", "request_hash"]
