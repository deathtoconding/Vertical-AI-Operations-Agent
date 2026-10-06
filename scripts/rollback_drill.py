#!/usr/bin/env python3
"""Rollback drill (DEV-004).

A rollback procedure that has never been executed is a paragraph, not a procedure. This script
executes it against a running candidate and records the evidence:

1. **capture** — read the current release and its health (the state we roll back *from*);
2. **inject** — add a simulated bad deployment through the sandbox intake and confirm the system
   degraded. A drill that "rolls back" a healthy system proves nothing;
3. **roll back** — use the same path production uses: the agent proposes
   ``deployment.rollback_simulation``, policy requires approval, a human (this script, acting as
   the approver) approves with the payload hash, the executor runs it, and the verification
   engine judges the result **independently of the executor**;
4. **compare** — read the deployment state and the incident's verification outcome afterwards
   and require the release to have changed back and the outcome to be `SUCCESS`;
5. **record** — write the drill as a JSON artefact: this is the release evidence attached to the
   deployment, referenced by the runbook.

The drill exits non-zero when recovery cannot be verified: an unverified rollback is a failed
rollback.

Usage::

    python scripts/rollback_drill.py
    python scripts/rollback_drill.py --base-url https://staging.example.invalid --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

TERMINAL_STATES = {"RESOLVED", "FAILED", "ESCALATED"}
DRILL_SCENARIO = "A"


@dataclass
class DrillStep:
    name: str
    passed: bool
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


class RollbackDrill:
    """Executes the documented rollback procedure and records what happened."""

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = 30.0) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout)

    # -- steps -------------------------------------------------------------- #

    def before_state(self) -> DrillStep:
        release = self._json(self.client.get("/api/v1/release/current"))
        sandbox = self._json(self.client.get("/api/v1/admin/sandbox"))
        active = release.get("active_release")
        ok = bool(active) and bool(release.get("healthy"))
        return DrillStep(
            "before",
            ok,
            f"release {active!r} healthy={release.get('healthy')} "
            f"(previous {release.get('previous_release')!r})",
            {"release": release, "sandbox": sandbox},
        )

    def inject_bad_release(self, before_release: str | None) -> DrillStep:
        response = self.client.post(
            "/api/v1/detection/simulate",
            json={"scenario": DRILL_SCENARIO, "orchestrate": True, "reset": True},
        )
        body = self._json(response)
        created = body.get("created_ids") or []
        degraded = self._json(self.client.get("/api/v1/release/current"))
        active = degraded.get("active_release")
        healthy = degraded.get("healthy")
        ok = (
            response.status_code == 200
            and bool(created)
            and healthy is False
            and active not in {None, before_release}
        )
        return DrillStep(
            "inject",
            ok,
            (
                f"scenario {DRILL_SCENARIO}: release moved to {active!r} and reports "
                f"healthy={healthy}; incident {created[0] if created else None}"
            ),
            {
                "simulate_status": response.status_code,
                "incident_ids": created,
                "release": degraded,
                "anomalies": body.get("anomalies"),
            },
        )

    def run_rollback(self, incident_id: str, *, timeout_seconds: float = 150.0) -> DrillStep:
        deadline = time.monotonic() + timeout_seconds
        approval: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            pending = self._pending_approvals(incident_id)
            if pending:
                approval = pending[0]
                break
            time.sleep(1.0)

        if approval is None:
            return DrillStep(
                "rollback",
                False,
                "no approval was requested — the rollback path did not engage",
                {"incident_id": incident_id},
            )

        decision = self.client.post(
            f"/api/v1/approvals/{approval['id']}/decision",
            json={
                "decision": "APPROVED",
                "payload_hash": approval["payload_hash"],
                "reason": "DEV-004 rollback drill (human approval step)",
            },
        )
        if decision.status_code not in {200, 201, 202}:
            return DrillStep(
                "rollback",
                False,
                f"approval refused with HTTP {decision.status_code}",
                {"approval_id": approval["id"], "body": self._json(decision)},
            )

        state = "UNKNOWN"
        while time.monotonic() < deadline:
            incident = self._incident(incident_id)
            state = str(incident.get("status") or "UNKNOWN")
            if state in TERMINAL_STATES:
                break
            time.sleep(1.0)

        incident = self._incident(incident_id)
        outcome = str(incident.get("verification_outcome") or "UNKNOWN")
        ok = state == "RESOLVED" and outcome.lower() == "success"
        return DrillStep(
            "rollback",
            ok,
            (
                f"incident {incident_id} is {state} with independent verification "
                f"{outcome} (approval {approval['id']}, "
                f"high-risk tool {approval.get('tool_name')!r})"
            ),
            {
                "approval_id": approval["id"],
                "tool_name": approval.get("tool_name"),
                "state": state,
                "verification_outcome": outcome,
                "audit_chain": self._json(self.client.get("/api/v1/audit/verify")),
            },
        )

    def after_state(self, before_release: str | None) -> DrillStep:
        release = self._json(self.client.get("/api/v1/release/current"))
        sandbox = self._json(self.client.get("/api/v1/admin/sandbox"))
        active = release.get("active_release")
        restored = active is not None and active != before_release
        healthy = release.get("healthy") is True
        rolled_back = bool(sandbox.get("rollback_count", 0))
        ok = restored and healthy and rolled_back
        return DrillStep(
            "after",
            ok,
            (
                f"release {before_release!r} → {active!r}, healthy={healthy}, "
                f"rollbacks recorded={sandbox.get('rollback_count')}"
            ),
            {"release": release, "sandbox": sandbox},
        )

    # -- helpers ------------------------------------------------------------ #

    def _incident(self, incident_id: str) -> dict[str, Any]:
        payload = self._json(self.client.get(f"/api/v1/incidents/{incident_id}"))
        return payload.get("incident") or {}

    def _pending_approvals(self, incident_id: str) -> list[dict[str, Any]]:
        payload = self._json(self.client.get("/api/v1/approvals", params={"pending_only": "true"}))
        items = payload.get("approvals") or []
        return [item for item in items if item.get("incident_id") == incident_id]

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {"data": payload}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--token", default=None)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    drill = RollbackDrill(args.base_url, token=args.token)
    steps: list[DrillStep] = []

    try:
        before = drill.before_state()
        steps.append(before)
        injected = drill.inject_bad_release(
            before.evidence.get("release", {}).get("active_release")
        )
        steps.append(injected)

        incident_ids = injected.evidence.get("incident_ids") or []
        if incident_ids:
            steps.append(drill.run_rollback(str(incident_ids[0])))
        else:
            steps.append(
                DrillStep(
                    "rollback",
                    False,
                    "no incident was created, so no rollback could be exercised",
                    {},
                )
            )
        steps.append(drill.after_state(before.evidence.get("release", {}).get("active_release")))
    except httpx.HTTPError as exc:
        print(f"ROLLBACK DRILL FAILED: {exc}", file=sys.stderr)
        return 2

    passed = all(step.passed for step in steps)
    report = {
        "base_url": args.base_url,
        "scenario": DRILL_SCENARIO,
        "passed": passed,
        "steps": [
            {
                "name": step.name,
                "passed": step.passed,
                "detail": step.detail,
                "evidence": step.evidence,
            }
            for step in steps
        ],
    }

    for step in steps:
        print(f"[{'PASS' if step.passed else 'FAIL'}] {step.name}: {step.detail}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"drill evidence written to {args.json}")

    if passed:
        print("ROLLBACK DRILL PASSED — recovery verified independently of the executor")
        return 0
    print("ROLLBACK DRILL FAILED", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
