"""Approval binding (OPS-061).

An approval that is not bound to a payload is a blank cheque. These tests attack the binding
from every angle the threat model names (T-09): a changed parameter, a changed tool, an
expired approval, a rejection, and a pending request that was never decided.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.actions.approval import ApprovalRequest, binds, build_approval_request
from app.domain.actions import Action, canonical_hash
from app.domain.enums import ActionStatus, ApprovalDecision, RiskLevel
from app.persistence.models.base import utcnow

pytestmark = [pytest.mark.story("OPS-061"), pytest.mark.unit]

PARAMS = {"target_release": "release-41", "reason": "error spike after release-42"}


def action(**overrides: object) -> Action:
    params = dict(overrides.pop("params", PARAMS))  # type: ignore[arg-type]
    tool_name = str(overrides.pop("tool_name", "deployment.rollback_simulation"))
    values: dict[str, object] = {
        "id": "ACT-1",
        "incident_id": "INC-1",
        "run_id": "RUN-1",
        "tool_name": tool_name,
        "risk": RiskLevel.HIGH,
        "permission": "actions.execute",
        "params": params,
        "canonical_hash": canonical_hash({"tool_name": tool_name, "params": params}),
        "idempotency_key": "idem-1",
        "requires_approval": True,
        "status": ActionStatus.AWAITING_APPROVAL,
    }
    values.update(overrides)
    return Action.model_validate(values)


class Approval:
    """Minimal stand-in for the persisted approval row the helper inspects."""

    def __init__(
        self,
        *,
        decision: ApprovalDecision = ApprovalDecision.APPROVED,
        payload_hash: str,
        expires_at: dt.datetime | None = None,
    ) -> None:
        self.decision = decision
        self.payload_hash = payload_hash
        self.expires_at = expires_at or utcnow() + dt.timedelta(minutes=5)


# --------------------------------------------------------------------------- #
# Request construction
# --------------------------------------------------------------------------- #


def test_request_is_bound_to_the_action_payload_and_expires() -> None:
    request = build_approval_request(action(), approval_id="APR-1", timeout_seconds=900)

    assert isinstance(request, ApprovalRequest)
    assert request.approval_id == "APR-1"
    assert request.values["action_id"] == "ACT-1"
    assert request.values["incident_id"] == "INC-1"
    assert request.values["run_id"] == "RUN-1"
    assert request.values["decision"] == ApprovalDecision.PENDING.value
    assert request.payload_hash == action().canonical_hash
    expires_at = request.values["expires_at"]
    assert isinstance(expires_at, dt.datetime)
    assert (expires_at - request.values["requested_at"]).total_seconds() == pytest.approx(900)


def test_parameter_order_does_not_change_the_binding() -> None:
    reordered = Action.model_validate(
        {
            **action().model_dump(),
            "params": {"reason": PARAMS["reason"], "target_release": PARAMS["target_release"]},
        }
    )
    assert reordered.canonical_hash == action().canonical_hash


# --------------------------------------------------------------------------- #
# Binding checks
# --------------------------------------------------------------------------- #


def test_approved_unexpired_approval_for_the_same_payload_binds() -> None:
    target = action()
    approval = Approval(payload_hash=target.canonical_hash)
    assert binds(approval, target) is True


def test_changing_a_parameter_invalidates_the_approval() -> None:
    target = action(params={**PARAMS, "target_release": "release-40"})
    approval = Approval(payload_hash=action().canonical_hash)
    assert binds(approval, target) is False, "approving release-41 must not execute release-40"


def test_changing_the_tool_invalidates_the_approval() -> None:
    target = action(tool_name="deployment.rollback_simulation", params={"target_release": "x"})
    other = Approval(payload_hash=action(tool_name="slack.notify").canonical_hash)
    assert binds(other, target) is False


def test_pending_approval_does_not_bind() -> None:
    target = action()
    approval = Approval(decision=ApprovalDecision.PENDING, payload_hash=target.canonical_hash)
    assert binds(approval, target) is False


def test_rejected_approval_does_not_bind() -> None:
    target = action()
    approval = Approval(decision=ApprovalDecision.REJECTED, payload_hash=target.canonical_hash)
    assert binds(approval, target) is False


def test_expired_approval_does_not_bind() -> None:
    target = action()
    approval = Approval(
        payload_hash=target.canonical_hash,
        expires_at=utcnow() - dt.timedelta(seconds=1),
    )
    assert binds(approval, target) is False, "consent for a stale incident is not consent"


def test_an_approval_without_an_expiry_does_not_bind() -> None:
    target = action()
    approval = Approval(payload_hash=target.canonical_hash, expires_at=utcnow())
    approval.expires_at = None  # type: ignore[assignment]
    assert binds(approval, target) is False, "a missing expiry must fail closed"


def test_a_tampered_action_keeps_its_own_hash_and_is_detected_by_the_executor() -> None:
    """``payload_is_intact`` is the complementary check: editing params without the hash fails."""
    tampered = Action.model_validate(
        {**action().model_dump(), "params": {**PARAMS, "reason": "escalate to release-40"}}
    )
    assert tampered.payload_is_intact is False
