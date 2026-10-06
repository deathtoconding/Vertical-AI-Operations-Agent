# Runbook — Excessive agent errors

**Alerts:** `AIOPSAgentRunFailureRateHigh`, `AIOPSInvestigationLatencyHigh`, `AIOPSEscalationSpike`
**Metrics:** `aiops_agent_run_failure_total{stage}`, `aiops_escalation_total{reason}`,
`aiops_agent_run_duration_seconds`, `aiops_investigation_duration_seconds`

## Symptoms
* `agent_run_failure_total` rising, labelled by stage (`investigation`, `planning`,
  `execution`, `verification`).
* Escalations spike with a common reason.
* Runs stall in a non-terminal state and get recovered repeatedly.

## Impact
Incidents are detected but not investigated or remediated; humans receive escalations
instead of proposals.

## Diagnosis
1. `GET /api/v1/agents/runs?state=FAILED&limit=50` — cluster by `failure_reason`.
2. Per stage:
   * **investigation** — evidence source failures, LLM errors, or grounding rejections
     (`aiops_diagnosis_grounding_failures_total`).
   * **planning** — `proposals_rejected_total` means the model is emitting unregistered
     tools or malformed params (prompt regression → check eval baseline).
   * **execution** — integration errors, timeouts, or policy denials
     (`aiops_authorization_denials_total`).
   * **verification** — `aiops_verification_total{outcome="UNKNOWN"}` usually points at missing
     metric data rather than an agent bug.
3. Compare with the last deploy: `GET /api/v1/release/current` and the change log.

## Mitigation
* If a single tool is failing, disable it via the registry policy
  (`AIOPS_DISABLED_TOOLS=jira.create_incident`) — the planner will then propose
  alternatives, and attempts are counted rather than silently skipped.
* If failures follow a deploy, roll back the application (`scripts/rollback_drill.py` shows
  the verified procedure) — the agent's own rollback tool is for the *operated* service.
* If runs are stuck, `POST /api/v1/agents/recovery` drains them safely (never re-executing a
  high-risk action without fresh approval).

## Verification
1. Failure rate returns below baseline for 30 minutes.
2. A synthetic incident completes the full lifecycle:
   `POST /api/v1/detection/simulate {"scenario": "A"}`.

## Prevention
Stage-level error budgets, eval regression gate on prompt changes, and a canary check that
runs the three MVP scenarios after every deploy (`scripts/verify_release.py`).
