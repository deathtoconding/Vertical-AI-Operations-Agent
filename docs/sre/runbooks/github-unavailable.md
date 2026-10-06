# Runbook — GitHub unavailable or rate-limited

**Alerts:** `AIOPSGitHubFailures`, `AIOPSGitHubRateLimitLow`
**Metrics:** `aiops_integration_request_total{system="github"}`, `aiops_github_rate_limit_remaining`

## Symptoms
* Evidence collected with `source="github"` is missing; incidents carry a degradation note
  `github: unavailable`.
* `aiops_evidence_source_failures_total{source="github"}` rising; `403`/`429` responses.
* Investigation hypotheses stop correlating with recent deployments.

## Impact
**Degraded, not failed.** All other evidence sources continue. Diagnosis quality drops
because deployment correlation is the strongest signal for error spikes; the diagnosis is
explicitly labelled as missing that correlation rather than silently asserting a cause.

## Diagnosis
1. `GET /api/v1/integrations/health` → GitHub component status and last error class.
2. If `rate_limit_remaining == 0`: wait for reset (`X-RateLimit-Reset`), or switch to a
   token with a higher quota. The client already honours `Retry-After`.
3. If `401/403` with remaining quota: token expired or scope revoked.
4. If connection errors: check egress/DNS from the pod.

## Mitigation
* Rotate the token in the secret manager; restart to pick it up.
* Temporarily raise `AIOPS_GITHUB_TIMEOUT_SECONDS` only if the provider is slow — never to
  mask a hard failure.
* If the outage is prolonged, note it on the incident; the agent will escalate instead of
  proposing a deployment-correlated remediation without evidence.

## Verification
1. `GET /api/v1/integrations/health` → GitHub `ok`.
2. Re-collect evidence for an open incident:
   `POST /api/v1/incidents/{id}/evidence/collect` → `github` evidence count > 0.

## Prevention
Token expiry alerting, secondary token support, and caching of repository metadata for the
length of an incident so a brief outage does not degrade a running investigation.
