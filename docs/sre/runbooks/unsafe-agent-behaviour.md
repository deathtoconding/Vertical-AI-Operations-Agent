# Runbook — Unsafe agent behaviour (SEV1)

**Alerts:** `AIOPSUnsafeActionAttempt`, `AIOPSAuditChainBroken`, `AIOPSAuthorizationDenialSpike`,
`AIOPSPromptInjectionDetected`
**Metrics:** `aiops_unsafe_action_attempts_total`, `aiops_authorization_denials_total`,
`aiops_prompt_injection_detected_total`

Trigger this runbook for **any** increment of `unsafe_action_attempts_total` on
`outcome="executed"`, any `audit_chain_broken`, or anything that looks like the agent
attempting something outside its domain.

## Symptoms

An alert from `AIOPSUnsafeActionAttempt`, `AIOPSAuditChainBroken` or
`AIOPSPromptInjectionDetected` fired, or an operator observed the agent attempting
something outside `docs/planning/domain.md` §5.

## Impact

The operated service may be changed without a valid human decision, or the audit record of
what happened may be incomplete. Both are release-blocking conditions.

## Immediate containment (first 5 minutes)
1. **Stop autonomy:** `POST /api/v1/admin/autonomy {"level": "observe_only"}` (admin). The
   agent keeps detecting and investigating but cannot execute any action. Audited.
2. **Stop executing actions** if containment is needed: `AIOPS_DISABLED_TOOLS=*` and restart.
   Detection and notification continue.
3. **Preserve evidence:** export the audit chain and the affected runs **before** any
   remediation:
   `GET /api/v1/audit/export?incident_id=... > audit-export.json`. Do not mutate the DB.
4. **Notify** the security owner and the incident commander; open a SEV1.

## Diagnosis (assess)
1. `GET /api/v1/audit/events?event_type=unsafe_action_attempted` — what was attempted, by
   which actor/run, with which payload.
2. `GET /api/v1/agents/runs/{id}` — the operational trace: which evidence introduced the
   instruction, which stage acted on it.
3. Inspect the evidence rows that entered the prompt (source, content hash). Untrusted
   content is retained so the injection can be reproduced in a test.
4. Determine the class:
   * **prompt injection** from external content → add the payload to
     `tests/security/prompt_injection/` and fix the framing/sanitiser.
   * **policy bug** → a rule permitted something it should not have; the fix is a rule plus
     a unit test in `tests/unit/policy/`.
   * **registry bug** → an undeclared capability was reachable; treat as critical and audit
     every invocation of it.
   * **credential abuse** → rotate the integration token involved immediately.

## Mitigation
1. Apply the code/config fix on a branch with a regression test that fails before the fix.
2. Rotate any credential that could have been exposed.
3. Re-enable autonomy stepwise: `approval_required` first, watch the security panel for
   24 h, then `selective_autonomy` only if the incident's action is complete.

## Verification
1. The new regression test fails against the old code and passes against the fixed code.
2. `verify_chain()` is `{valid: true}` and the exported chain matches the live chain head.
3. Zero `unsafe_action_attempts_total` increments for 24 h after re-enabling.
4. At least one of: new alert, new policy rule, new runbook step — a review with no artefact
   is incomplete.

## Prevention
Zero-tolerance alerting on the two security SLIs; auto-disable of autonomy on threshold
breach; the full prompt-injection corpus in CI; and review of every new tool for its risk
and permission declarations.
