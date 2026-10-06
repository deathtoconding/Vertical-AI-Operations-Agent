# Runbook — Deployment Rollback (DEV-004)

**Scope:** rolling back a bad release, whether the rollback is proposed by the agent or triggered by
a human. Covers the simulated provider used by this repository and the changes needed when
`AIOPS_INTEGRATIONS_MODE=live` points at a real deployment system.

**Related:** `docs/sre/runbooks/excessive-agent-errors.md` (when the agent itself is failing),
`docs/architecture/verification.md` (why a 200 is not evidence), `scripts/rollback_drill.py`.

---

## Symptoms

- `aiops_incidents_created_total{severity="SEV1"|"SEV2"}` rising with error-rate or latency
  anomalies rather than an isolated blip.
- `aiops_tool_invocations_total{tool="deployment.rollback_simulation",outcome="awaiting_approval"}`
  climbing: the agent is proposing rollbacks and waiting for a human.
- An incident stuck in `WAITING_APPROVAL` past the approval timeout, or in `VERIFYING` with
  `aiops_verification_total{outcome="unknown"}` rising.
- A release appeared in `GET /api/v1/release/current` (`active_release` changed) within minutes of
  the incident onset.
- Customer-visible errors: elevated 5xx rate, timeouts, or failed checkouts on the operated service.

## Impact

- **User impact:** degraded or failed requests for every user of the affected service; a payment
  anomaly may mean rejected charges and abandoned baskets.
- **Business impact:** lost conversions for the duration of the fault plus the time to detect,
  approve and verify recovery.
- **Blast radius:** the deployed release, not the platform — a rollback is a targeted action and is
  the reason it is the first remediation the agent proposes.
- **Agent impact:** if the rollback fails verification, the incident escalates to a human
  (`ESCALATED`) rather than being marked resolved. That is the intended behaviour; a stuck
  `ESCALATED` incident is a signal to act, not a defect.

## Diagnosis

1. **Confirm it is a release regression, not capacity or a dependency.** Compare the metric family
   that triggered the incident against the release timeline:

   ```bash
   curl -fsS "$BASE_URL/api/v1/release/current" | jq
   curl -fsS "$BASE_URL/api/v1/incidents?status=OPEN" | jq '.incidents[] | {id, severity, status, metric}'
   ```

2. **Read the incident's evidence**, not only its summary. The deployment and GitHub evidence items
   carry the release id, commit and timestamp; the diagnosis cites the evidence ids it used:

   ```bash
   curl -fsS "$BASE_URL/api/v1/incidents/$INCIDENT_ID" | jq '.incident.diagnosis'
   curl -fsS "$BASE_URL/api/v1/incidents/$INCIDENT_ID/evidence" | jq '.evidence[] | {id, source, kind, summary}'
   ```

3. **Check what the agent proposed and what policy decided** — a `DENY` has a reason, and an
   `ALLOW` on a high-risk tool would be a policy defect worth paging for:

   ```bash
   curl -fsS "$BASE_URL/api/v1/approvals?pending_only=true" | jq '.approvals[] | {id, tool_name, risk, payload_hash, incident_id}'
   ```

4. **Inspect the failure signal** in the alert rules that fired: `ApiErrorBudgetBurning`,
   `ApiLatencyHigh`, `IntegrationFailureRateHigh`, or `ToolFailureRateHigh`
   (`infra/deployment/prometheus/rules.yml`).

5. **Rule out the rollback path itself being unavailable:** if the deployment provider is
   unreachable, `aiops_integration_request_total{system="deployment",outcome!="success"}` rises and
   the action fails — see `docs/sre/runbooks/github-unavailable.md` for the equivalent handling of a
   provider outage.

## Mitigation

1. **Let the agent finish its gate.** A HIGH-risk rollback requires an approval bound to the exact
   payload hash; approving a stale tab's hash is refused by design. Approve from a freshly loaded
   page:

   ```bash
   APPROVAL=$(curl -fsS "$BASE_URL/api/v1/approvals?pending_only=true" | jq -r '.approvals[0]')
   ID=$(jq -r '.id' <<<"$APPROVAL"); HASH=$(jq -r '.payload_hash' <<<"$APPROVAL")
   curl -fsS -X POST "$BASE_URL/api/v1/approvals/$ID/decision" \
     -H "Authorization: Bearer $OPERATOR_TOKEN" -H 'Content-Type: application/json' \
     -d "{\"decision\":\"APPROVED\",\"payload_hash\":\"$HASH\",\"reason\":\"SEV2 error spike on release-42\"}"
   ```

2. **Or run the drill path manually** (same code path the CD pipeline uses):

   ```bash
   python scripts/rollback_drill.py --base-url "$BASE_URL" --token "$ADMIN_TOKEN" \
     --json rollback-report.json
   ```

   Use an admin token when a rollback has already happened in the last hour: policy escalates the
   second high-risk action on the same fault to `critical` (`repeat_high_risk_action`), and a
   critical-risk proposal needs an admin. The drill says so in its failure detail — it reports the
   policy reason it was refused rather than pretending no approval was needed. The drill consumes a
   rollback from the guardrail budget, and it repairs its own baseline (a dirty sandbox left by an
   earlier scenario is reset and recorded), so it is safe to re-run.

3. **If the agent cannot act** (policy denial, approval timeout, LLM outage), roll back with the
   platform's own tooling and record it: an unrecorded rollback leaves the incident's timeline
   lying, and the next investigation will read that history.

4. **Reduce autonomy if the agent is part of the problem** — `observe_only` denies every execution
   while keeping detection and investigation running:

   ```bash
   curl -fsS -X POST "$BASE_URL/api/v1/admin/autonomy" \
     -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
     -d '{"level":"observe_only","reason":"rollback verification failing"}'
   ```

5. **Escalate past the loop limit.** More than `AIOPS_MAX_ROLLBACKS_PER_HOUR` rollbacks in an hour
   are denied by policy; at that point the release is not the problem and the incident needs a human.

## Verification

Recovery is a claim that must be checked independently — never infer it from the rollback's HTTP
status:

1. **Deployment state:** `GET /api/v1/release/current` shows the previous release `active_release`
   and `healthy: true`.

   ```bash
   curl -fsS "$BASE_URL/api/v1/release/current" | jq '{active_release, previous_release, healthy}'
   ```

2. **Verification outcome:** the incident's `verification_outcome` is `success`, produced by the
   checks declared *before* execution (`release_active`, `deployment_healthy`, `metric_recovered`):

   ```bash
   curl -fsS "$BASE_URL/api/v1/incidents/$INCIDENT_ID" | jq '{status, verification_outcome}'
   ```

3. **Metric recovery:** the triggering metric returned below its declared threshold in
   `aiops_verification_total{outcome="success"}` and the golden-signal panels.

4. **Audit integrity:** the chain still verifies, which proves the rollback and its approval were
   recorded rather than rewritten:

   ```bash
   curl -fsS "$BASE_URL/api/v1/audit/verify" | jq '{valid, entries}'
   ```

5. **The right number of incidents.** Deduplication applies while an incident is *still open*:
   `aiops_incidents_deduplicated_total` incrementing during an active incident is normal. Once an
   incident reaches `RESOLVED`/`ESCALATED`, the same fault recurring raises a **new** incident —
   that is how a rollback which did not hold becomes visible, so a second incident minutes later is
   expected when the first one is finished, and unexpected while it is still open.

6. **Record the evidence** — attach `rollback-report.json` from `scripts/rollback_drill.py` (or the
   release report from `scripts/verify_release.py`) to the incident. A rollback without its
   verification evidence is an unverified rollback, and the drill fails in that case by design.
