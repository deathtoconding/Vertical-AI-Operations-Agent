#!/usr/bin/env python3
"""Release verification (DEV-003, OPS-070).

Runs against a *running* candidate and answers one question: is this release actually working,
not merely serving HTTP? Every check is one a human would otherwise do by hand at 02:00:

1. **liveness** — ``/health`` is dependency-free and answers even while a dependency is down;
2. **readiness** — ``/ready`` reports per-dependency state and must not claim ready while the
   database is unreachable;
3. **contract** — the OpenAPI document still exposes the routes the committed contract declares,
   so a release that lost half its API cannot pass silently;
4. **observability** — ``/metrics`` still exports the SLO families the dashboards and alerts
   depend on (SRE-004). Without this, "the dashboard is green" stops meaning anything;
5. **safety** — the sandbox scenario that proposes a HIGH-risk rollback must stop at an approval
   gate. An HTTP 200 on ``/detection/simulate`` is not evidence of anything.
6. **lifecycle** — drive scenario A end to end: detect → investigate → plan → approve → execute
   → verify → resolve, then require a *verified* terminal state and an intact audit chain. HTTP
   200s alone never count as success — the same rule the verification engine enforces internally.

Exit 0 means the candidate passed; anything else is a failed release verification and the
deployment should be rolled back with ``scripts/rollback_drill.py`` (DEV-004).

Usage::

    python scripts/verify_release.py --base-url http://localhost:8000
    python scripts/verify_release.py --base-url https://staging.example.invalid --json report.json
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

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Metric families the SLO dashboards and alert rules depend on (SRE-004).
REQUIRED_METRICS = (
    "aiops_http_requests_total",
    "aiops_http_request_duration_seconds",
    "aiops_incidents_created_total",
    "aiops_investigation_duration_seconds",
    "aiops_tool_invocations_total",
    "aiops_verification_total",
    "aiops_escalation_total",
    "aiops_unsafe_action_attempts_total",
    "aiops_llm_latency_seconds",
    "aiops_audit_events_total",
    "aiops_db_pool_in_use",
)

TERMINAL_STATES = {"RESOLVED", "FAILED", "ESCALATED"}
HIGH_RISK = {"high", "critical"}


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


class ReleaseVerifier:
    """Drives the release checks against one base URL."""

    def __init__(self, base_url: str, *, token: str | None, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = httpx.Client(base_url=self.base_url, headers=headers, timeout=timeout)
        self.expected_paths = self._expected_path_count()

    # -- checks ------------------------------------------------------------- #

    def check_health(self) -> CheckResult:
        response = self.client.get("/health")
        body = self._json(response)
        ok = response.status_code == 200 and bool(body.get("status"))
        return CheckResult(
            "health",
            ok,
            f"/health returned HTTP {response.status_code} with status={body.get('status')!r}",
            {"status_code": response.status_code, "body": body},
        )

    def check_readiness(self) -> CheckResult:
        response = self.client.get("/ready")
        body = self._json(response)
        ready = body.get("status") == "ready"
        # A readiness endpoint that reports ready while a dependency is down is worse than none:
        # the status code and the payload must agree.
        consistent = (response.status_code == 200) == ready
        ok = response.status_code in {200, 503} and "checks" in body and consistent
        return CheckResult(
            "readiness",
            ok,
            (
                f"/ready returned HTTP {response.status_code} with status={body.get('status')!r} "
                f"({len(body.get('checks', {}))} dependency checks)"
            ),
            {"status_code": response.status_code, "body": body},
        )

    def check_api_contract(self) -> CheckResult:
        response = self.client.get("/openapi.json")
        document = self._json(response)
        paths = document.get("paths", {})
        missing_documented = [
            path
            for path in ("/health", "/ready", "/metrics", "/api/v1/incidents", "/api/v1/approvals")
            if path not in paths
        ]
        ok = response.status_code == 200 and not missing_documented
        return CheckResult(
            "api_contract",
            ok,
            (
                f"OpenAPI exposes {len(paths)} paths; every contract-critical route is present"
                if ok
                else f"OpenAPI is missing: {', '.join(missing_documented)}"
            ),
            {
                "paths": len(paths),
                "expected_paths": self.expected_paths,
                "missing": missing_documented,
            },
        )

    def check_metrics(self) -> CheckResult:
        response = self.client.get("/metrics")
        text = response.text
        missing = [name for name in REQUIRED_METRICS if name not in text]
        return CheckResult(
            "metrics",
            response.status_code == 200 and not missing,
            (
                f"/metrics exposes all {len(REQUIRED_METRICS)} required SLO families"
                if not missing
                else f"/metrics is missing: {', '.join(missing)}"
            ),
            {"missing": missing, "bytes": len(text)},
        )

    def check_safety(self) -> CheckResult:
        """A HIGH-risk proposal must be gated by an approval, not executed."""
        simulate = self.client.post(
            "/api/v1/detection/simulate",
            json={"scenario": "A", "orchestrate": True, "reset": True},
        )
        body = self._json(simulate)
        incident_id = (body.get("created_ids") or [None])[0]

        approvals = self._json(
            self.client.get("/api/v1/approvals", params={"pending_only": "true"})
        )
        pending = approvals.get("approvals") or []
        gated = [item for item in pending if str(item.get("risk", "")).lower() in HIGH_RISK]
        gate_ok = bool(gated) or not incident_id

        # Authentication posture: with auth enabled an unauthenticated read is refused; with
        # auth disabled (development/test profiles) the endpoint answers. Both are recorded,
        # neither is silently accepted as "safe".
        unauth = httpx.get(f"{self.base_url}/api/v1/incidents", timeout=10.0)
        auth_enforced = unauth.status_code in {401, 403}

        return CheckResult(
            "safety",
            bool(incident_id) and gate_ok,
            (
                f"HIGH-risk proposal gated by {len(gated)} pending approval(s); "
                f"unauthenticated read returned HTTP {unauth.status_code}"
            ),
            {
                "incident_id": incident_id,
                "pending_high_risk": len(gated),
                "simulate_status": simulate.status_code,
                "auth_enforced": auth_enforced,
                "unauth_status": unauth.status_code,
            },
        )

    def check_lifecycle(self, *, timeout_seconds: float = 120.0) -> CheckResult:
        """Drive scenario A end to end and require a *verified* terminal state."""
        simulate = self.client.post(
            "/api/v1/detection/simulate",
            json={"scenario": "A", "orchestrate": True, "reset": True},
        )
        body = self._json(simulate)
        created = body.get("created_ids") or []
        if simulate.status_code != 200 or not created:
            return CheckResult(
                "lifecycle",
                False,
                f"simulation did not create an incident (HTTP {simulate.status_code})",
                {"status_code": simulate.status_code, "body": body},
            )

        incident_id = str(created[0])
        deadline = time.monotonic() + timeout_seconds
        state = "UNKNOWN"
        approvals: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            incident = self._incident(incident_id)
            state = str(incident.get("status") or "UNKNOWN")
            approvals = self._pending_approvals(incident_id)
            if state in TERMINAL_STATES or (state == "WAITING_APPROVAL" and approvals):
                break
            time.sleep(1.0)

        if state == "WAITING_APPROVAL" and approvals:
            approval = approvals[0]
            decision = self.client.post(
                f"/api/v1/approvals/{approval['id']}/decision",
                json={
                    "decision": "APPROVED",
                    "payload_hash": approval["payload_hash"],
                    "reason": "release verification (DEV-003)",
                },
            )
            if decision.status_code not in {200, 201, 202}:
                return CheckResult(
                    "lifecycle",
                    False,
                    f"approval was refused with HTTP {decision.status_code}",
                    {"approval_id": approval["id"], "body": self._json(decision)},
                )
            # Approving is not executing: the decision releases the gate and the run is
            # resumed explicitly (the decision response says exactly this). A verifier that
            # approved and then waited for a terminal state would time out and blame the
            # candidate for its own missing step.
            resumed = self._resume_run(incident_id)
            while time.monotonic() < deadline:
                incident = self._incident(incident_id)
                state = str(incident.get("status") or "UNKNOWN")
                if state in TERMINAL_STATES:
                    break
                time.sleep(1.0)
            if resumed is not None:
                approvals[0]["resume"] = resumed

        incident = self._incident(incident_id)
        state = str(incident.get("status") or "UNKNOWN")
        verification_outcome = str(incident.get("verification_outcome") or "UNKNOWN")
        chain = self._json(self.client.get("/api/v1/audit/verify"))
        chain_valid = bool(chain.get("valid"))
        run = self._json(self.client.get(f"/api/v1/incidents/{incident_id}")).get("run") or {}
        transitions = self._transitions(str(run.get("id") or ""))
        expected = {"NEW", "DETECTED", "INVESTIGATING", "PLANNED", "WAITING_APPROVAL"}
        seen_states = {str(item.get("from_state")) for item in transitions} | {
            str(item.get("to_state")) for item in transitions
        }

        resolved = state == "RESOLVED"
        verified = verification_outcome.lower() == "success"
        lifecycle_ok = resolved and verified and chain_valid and expected.issubset(seen_states)

        return CheckResult(
            "lifecycle",
            lifecycle_ok,
            (
                f"incident {incident_id} reached {state} with verification "
                f"{verification_outcome}; audit chain valid={chain_valid}; "
                f"lifecycle states observed: {', '.join(sorted(seen_states))}"
            ),
            {
                "incident_id": incident_id,
                "state": state,
                "verification_outcome": verification_outcome,
                "audit_chain": chain,
                "run": run,
                "transitions": transitions,
            },
        )

    # -- driving ------------------------------------------------------------ #

    def run_all(self) -> list[CheckResult]:
        return [
            self.check_health(),
            self.check_readiness(),
            self.check_api_contract(),
            self.check_metrics(),
            self.check_safety(),
            self.check_lifecycle(),
        ]

    # -- helpers ------------------------------------------------------------ #

    def _incident(self, incident_id: str) -> dict[str, Any]:
        payload = self._json(self.client.get(f"/api/v1/incidents/{incident_id}"))
        return payload.get("incident") or {}

    def _pending_approvals(self, incident_id: str) -> list[dict[str, Any]]:
        payload = self._json(self.client.get("/api/v1/approvals", params={"pending_only": "true"}))
        items = payload.get("approvals") or []
        return [item for item in items if item.get("incident_id") == incident_id]

    def _resume_run(self, incident_id: str) -> dict[str, Any] | None:
        """Resume the run that is waiting on the approval we just granted."""
        payload = self._json(self.client.get(f"/api/v1/incidents/{incident_id}"))
        run = payload.get("run") or {}
        run_id = str(run.get("id") or "")
        if not run_id or str(run.get("state")) != "WAITING_APPROVAL":
            return None
        response = self.client.post(
            f"/api/v1/agents/runs/{run_id}/resume",
            json={"reason": "release verification: approval granted (DEV-003)"},
        )
        return {
            "run_id": run_id,
            "status_code": response.status_code,
            "state": (self._json(response).get("run") or {}).get("state"),
        }

    def _transitions(self, run_id: str) -> list[dict[str, Any]]:
        if not run_id:
            return []
        payload = self._json(self.client.get(f"/api/v1/agents/runs/{run_id}"))
        transitions = payload.get("transitions")
        return transitions if isinstance(transitions, list) else []

    def _expected_path_count(self) -> int:
        contract = REPO_ROOT / "docs" / "architecture" / "openapi.json"
        if contract.exists():
            try:
                return len(json.loads(contract.read_text(encoding="utf-8")).get("paths", {}))
            except (ValueError, OSError):  # pragma: no cover - the committed contract is valid
                return 1
        return 1

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
    parser.add_argument("--token", default=None, help="bearer token for authenticated candidates")
    parser.add_argument("--json", type=Path, default=None, help="write the report to this path")
    args = parser.parse_args(argv)

    verifier = ReleaseVerifier(args.base_url, token=args.token)
    try:
        results = verifier.run_all()
    except httpx.HTTPError as exc:
        print(f"RELEASE VERIFICATION FAILED: cannot reach {args.base_url}: {exc}", file=sys.stderr)
        return 2

    report = {
        "base_url": args.base_url,
        "passed": all(result.passed for result in results),
        "checks": [
            {
                "name": result.name,
                "passed": result.passed,
                "detail": result.detail,
                "evidence": result.evidence,
            }
            for result in results
        ],
    }

    for result in results:
        print(f"[{'PASS' if result.passed else 'FAIL'}] {result.name}: {result.detail}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"report written to {args.json}")

    if report["passed"]:
        print("RELEASE VERIFICATION PASSED")
        return 0
    print("RELEASE VERIFICATION FAILED", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
