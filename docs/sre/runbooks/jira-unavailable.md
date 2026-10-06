# Runbook — Jira unavailable or rejecting requests

**Alerts:** `AIOPSJiraFailures`, `AIOPSActionFailureRateHigh{tool="jira.create_incident"}`
**Metrics:** `aiops_integration_request_total{system="jira"}`, `aiops_action_failures_total`, `aiops_action_failures_total{tool}`

## Symptoms
* `jira.create_incident` actions end in `FAILED`; the run escalates with
  `reason="action_failed"`.
* Idempotent replays return the original key when Jira recovers (no duplicate tickets).

## Impact
Incident management in Jira is delayed. **The incident itself is not lost** — it is durable
in PostgreSQL before the Jira call, and Slack notification is a separate tool, so on-call is
still paged.

## Diagnosis
1. `GET /api/v1/integrations/health` → Jira component status.
2. `401` → token/email invalid. `404` → project key wrong or the issue type is missing.
   `429` → rate limited (backoff handles it). `5xx` → provider incident.
3. Check the idempotency table for `scope="jira.create_incident"` rows stuck without an
   `external_id` — those are the replays to run once Jira is back.

## Mitigation
* Fix credentials/URL in the secret manager, then replay:
  `POST /api/v1/actions/{action_id}/retry` (idempotent — safe by construction).
* If Jira will be down for long, escalate manually and annotate the incident; do not disable
  the tool (a silent skip would violate "no silent success").

## Verification
1. The escalation action created a Jira issue: `GET /api/v1/actions/{id}` shows
   `external_id` populated and verification `SUCCESS`.
2. No duplicate issues exist for the same incident (idempotency check).

## Prevention
Provider status alerting and a Jira-outage switch that routes notifications to Slack only,
recorded on the incident timeline as an explicit degradation.
