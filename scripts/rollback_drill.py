#!/usr/bin/env python3
"""Rollback drill (DEV-004).

A rollback procedure that has never been executed is a paragraph, not a procedure. This script
executes it against a running candidate and records the evidence:

1. **capture** — read the current release and its health (the state we roll back *from*);
1. **baseline** — a drill needs a known-good starting point. If a previous drill (or an earlier
   scenario run) left the sandbox degraded, reset it and record that the baseline was repaired;
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

Preconditions and guardrails, stated because a drill that hides them is not evidence:

* the candidate must be able to reach a **healthy baseline** — if an earlier scenario left the
  sandbox degraded, the drill resets it and records that it did;
* the token must be allowed to *request* the risk the rollback carries. A rollback proposed within
  an hour of a previous high-risk action escalates to ``critical`` (``repeat_high_risk_action``),
  which needs an **admin** token; with a lesser role the drill fails and reports the policy reason
  instead of pretending the gate was never reached;
* a runtime penalty of up to ``--approval-timeout`` seconds when no approval can appear.

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
#: Injection scenarios, in order. A drill must be *repeatable* against a live candidate: a
#: resolved incident suppresses a new detection inside the dedup window, so if the first fault
#: shape deduplicates into an incident that is already finished, the next shape (a different
#: metric, hence a different dedup key) is injected instead. Both produce a rollback proposal.
DRILL_SCENARIOS = ("A", "B")
DRILL_SCENARIO = DRILL_SCENARIOS[0]


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

    def prepare_baseline(self) -> DrillStep:
        """Establish a known-good starting point, declaring it when one has to be restored.

        The drill runs against the sandbox, and a sandbox is shared with whatever ran before it —
        a previous drill, a scenario walkthrough, a demo. Starting from an already-degraded pool
        would silently turn "rollback restored the release" into "the release was never healthy",
        so the baseline is established explicitly and recorded in the artefact.
        """
        release = self._json(self.client.get("/api/v1/release/current"))
        if release.get("healthy") is True:
            return DrillStep(
                "baseline",
                True,
                f"candidate already at a healthy baseline ({release.get('active_release')!r})",
                {"reset": False, "release": release},
            )
        response = self.client.post("/api/v1/admin/sandbox/reset", params={"scenario": "normal"})
        after = self._json(self.client.get("/api/v1/release/current"))
        ok = response.status_code in {200, 201, 202} and after.get("healthy") is True
        return DrillStep(
            "baseline",
            ok,
            (
                f"reset the sandbox to a healthy baseline (HTTP {response.status_code}); "
                f"release {after.get('active_release')!r} healthy={after.get('healthy')}"
            ),
            {"reset": True, "status_code": response.status_code, "release": after},
        )

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

    def inject_bad_release(
        self, before_release: str | None, *, scenario: str = DRILL_SCENARIO
    ) -> DrillStep:
        response = self.client.post(
            "/api/v1/detection/simulate",
            json={"scenario": scenario, "orchestrate": True, "reset": True},
        )
        body = self._json(response)
        created = [
            str(item)
            for item in [
                *(body.get("incidents_created") or body.get("created_ids") or []),
                *(body.get("incidents_deduplicated") or []),
            ]
        ]
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
                f"scenario {scenario}: release moved to {active!r} and reports "
                f"healthy={healthy}; incident {created[0] if created else None}"
            ),
            {
                "scenario": scenario,
                "deduplicated": not body.get("incidents_created"),
                "simulate_status": response.status_code,
                "incident_ids": created,
                "release": degraded,
                "anomalies": body.get("anomalies"),
            },
        )

    def incident_status(self, incident_id: str) -> str:
        """The incident's status, or ``UNKNOWN`` when it cannot be read."""
        return str(self._incident(incident_id).get("status") or "UNKNOWN")

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
            # "No approval appeared" has causes an operator must be able to act on: the policy
            # guardrail refused the proposal (e.g. max_rollbacks_per_hour), or the run never got
            # as far as proposing. Read the audit trail and say which.
            diagnosis = self._explain_missing_rollback(incident_id)
            refused = diagnosis["rejected"]
            reason = refused[-1] if refused else "no proposal reached the approval gate"
            return DrillStep(
                "rollback",
                False,
                f"no approval was requested — the rollback path did not engage ({reason})",
                {"incident_id": incident_id, "diagnosis": diagnosis},
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

        # The approval releases the gate; the run must then be resumed before the executor
        # acts. Without this the drill would wait for a terminal state that can never arrive.
        resumed = self._resume_run(incident_id)

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
                "resume": resumed,
                "state": state,
                "verification_outcome": outcome,
                "audit_chain": self._json(self.client.get("/api/v1/audit/verify")),
            },
        )

    def after_state(
        self, before_release: str | None, injected_release: str | None = None
    ) -> DrillStep:
        """Confirm the drill restored the pre-drill state and left nothing degraded behind.

        A rollback drill succeeds when the system is *back to where it was*, so the assertion is
        ``active == before_release`` (and therefore no longer the injected release) — not merely
        "the release string changed", which a second, unrelated deploy would also satisfy.
        """
        release = self._json(self.client.get("/api/v1/release/current"))
        sandbox = self._json(self.client.get("/api/v1/admin/sandbox"))
        active = release.get("active_release")
        restored = active is not None and (before_release is None or active == before_release)
        moved_off_bad = injected_release is None or active != injected_release
        healthy = release.get("healthy") is True
        rolled_back = bool(sandbox.get("rollback_count", 0))
        ok = restored and moved_off_bad and healthy and rolled_back
        return DrillStep(
            "after",
            ok,
            (
                f"release {before_release!r} → {injected_release!r} (injected) → {active!r}, "
                f"healthy={healthy}, rollbacks recorded={sandbox.get('rollback_count')}"
            ),
            {"release": release, "sandbox": sandbox, "injected_release": injected_release},
        )

    # -- helpers ------------------------------------------------------------ #

    def _incident(self, incident_id: str) -> dict[str, Any]:
        payload = self._json(self.client.get(f"/api/v1/incidents/{incident_id}"))
        return payload.get("incident") or {}

    def _resume_run(self, incident_id: str) -> dict[str, Any] | None:
        """Resume the run the approval was waiting on (the human step, then the machine step)."""
        payload = self._json(self.client.get(f"/api/v1/incidents/{incident_id}"))
        run = payload.get("run") or {}
        run_id = str(run.get("id") or "")
        if not run_id or str(run.get("state")) != "WAITING_APPROVAL":
            return None
        response = self.client.post(
            f"/api/v1/agents/runs/{run_id}/resume",
            json={"reason": "DEV-004 rollback drill: approval granted, executing"},
        )
        return {
            "run_id": run_id,
            "status_code": response.status_code,
            "state": (self._json(response).get("run") or {}).get("state"),
        }

    def _explain_missing_rollback(self, incident_id: str) -> dict[str, Any]:
        """Why the rollback never reached the approval gate, read from the run itself.

        A drill that says "no approval appeared" without saying why is a drill an operator cannot
        act on. The run records its plan, including the proposals policy refused and the reason.
        """
        detail = self._json(self.client.get(f"/api/v1/incidents/{incident_id}"))
        run = detail.get("run") or {}
        run_id = str(run.get("id") or "")
        plan: dict[str, Any] = {}
        notes: list[Any] = []
        state = str(run.get("state") or "UNKNOWN")
        if run_id:
            payload = self._json(self.client.get(f"/api/v1/agents/runs/{run_id}"))
            run_payload = payload.get("run") or {}
            plan = run_payload.get("plan") or {}
            notes = run_payload.get("notes") or []
            state = str(run_payload.get("state") or state)

        refused = [
            f"{item.get('tool_name')}: {item.get('reason')}"
            for item in (plan.get("rejected") or [])
            if item.get("reason")
        ]
        return {
            "incident_id": incident_id,
            "run_id": run_id or None,
            "run_state": state,
            "accepted": len(plan.get("accepted") or []),
            "rejected": refused,
            "notes": notes,
            "hint": (
                "a policy DENY (for example max_rollbacks_per_hour, or a repeat high-risk action "
                "inside the guardrail window) refuses the proposal by design; raise the guardrail "
                "deliberately or run the drill against an environment that has not spent it"
            ),
        }

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
    parser.add_argument(
        "--approval-timeout",
        type=float,
        default=150.0,
        help="seconds to wait for the approval gate to appear (default: 150)",
    )
    args = parser.parse_args(argv)

    drill = RollbackDrill(args.base_url, token=args.token)
    steps: list[DrillStep] = []

    try:
        steps.append(drill.prepare_baseline())
        before = drill.before_state()
        steps.append(before)
        baseline_release = before.evidence.get("release", {}).get("active_release")

        injected: DrillStep | None = None
        for scenario in DRILL_SCENARIOS:
            candidate = drill.inject_bad_release(baseline_release, scenario=scenario)
            steps.append(candidate)
            candidate_ids = candidate.evidence.get("incident_ids") or []
            if not candidate.passed or not candidate_ids:
                continue
            # A *fresh* incident that finished without an approval is not a reason to try another
            # scenario: the rollback step must diagnose why the approval never appeared (policy
            # refusing the proposal is the common case).
            state = drill.incident_status(str(candidate_ids[0]))
            deduplicated = bool(candidate.evidence.get("deduplicated"))
            if state in TERMINAL_STATES and deduplicated:
                # A detection that deduplicated into a *finished* incident cannot exercise the
                # rollback path — the run it belonged to is already over. Say so, and inject the
                # next fault shape (different metric ⇒ different dedup key) rather than waiting
                # for a window to expire or silently reporting a pass.
                # The detection was folded into an incident whose run is already over, so this
                # fault shape cannot exercise the rollback path. Try the next shape (different
                # metric ⇒ different dedup key) instead of waiting for a window to expire.
                steps.append(
                    DrillStep(
                        "inject",
                        False,
                        (
                            f"scenario {scenario} deduplicated into {candidate_ids[0]}, which is "
                            f"already {state}; trying the next scenario"
                        ),
                        {
                            "scenario": scenario,
                            "incident_id": candidate_ids[0],
                            "state": state,
                            "deduplicated": True,
                        },
                    )
                )
                continue
            injected = candidate
            break

        if injected is None:
            steps.append(
                DrillStep(
                    "rollback",
                    False,
                    "no injection produced an open incident, so the rollback path could not be "
                    "exercised",
                    {},
                )
            )
            injected = DrillStep(
                "inject",
                False,
                "no usable injection",
                {},
            )

        incident_ids = injected.evidence.get("incident_ids") or []
        if incident_ids:
            steps.append(
                drill.run_rollback(str(incident_ids[0]), timeout_seconds=args.approval_timeout)
            )
        else:
            steps.append(
                DrillStep(
                    "rollback",
                    False,
                    "no incident was created, so no rollback could be exercised",
                    {},
                )
            )
        steps.append(
            drill.after_state(
                before.evidence.get("release", {}).get("active_release"),
                injected.evidence.get("release", {}).get("active_release"),
            )
        )
    except httpx.HTTPError as exc:
        print(f"ROLLBACK DRILL FAILED: {exc}", file=sys.stderr)
        return 2

    passed = all(step.passed for step in steps)
    report = {
        "base_url": args.base_url,
        "scenarios": [step.evidence["scenario"] for step in steps if step.evidence.get("scenario")]
        or list(DRILL_SCENARIOS),
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
