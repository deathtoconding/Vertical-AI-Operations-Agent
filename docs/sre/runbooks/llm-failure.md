# Runbook — LLM failure or degradation

**Alerts:** `AIOPSLLMErrorRateHigh`, `AIOPSLLMLatencyHigh`, `AIOPSReasonerFallbackSpike`
**Metrics:** `aiops_llm_error_total{reason}`, `aiops_llm_latency_seconds`, `aiops_reasoner_fallback_total`

## Symptoms
* Diagnoses carry `reasoner: "deterministic"` with `degraded_reason` set.
* `llm_error_total{reason="timeout"|"auth"|"rate_limit"|"bad_response"}` rising.
* Investigation latency unchanged, but diagnosis confidence is lower and hypotheses are
  coarser.

## Impact
Investigations still complete (deterministic fallback) but quality drops: weaker
correlation, fewer alternative hypotheses, coarser severity reasoning. **Actions are
unaffected and remain policy-gated** — this is the point of ADR-0002.

## Diagnosis
1. `GET /api/v1/ready` → `llm` component status.
2. Check `llm_error_total` by reason:
   * `auth` → key rotated/revoked.
   * `rate_limit` → provider quota; check `Retry-After` handling.
   * `timeout` → provider latency or an oversized prompt (inspect evidence volume).
   * `bad_response` → schema violation; the response is logged (redacted) for review.
3. Confirm the prompt artefact version (`prompts/investigation/system.md`) was not changed
   by a recent deploy.

## Mitigation
* `auth`/`rate_limit`: rotate or upgrade the key; the agent continues on fallback meanwhile.
* `timeout`: reduce evidence volume via `AIOPS_EVIDENCE_MAX_ITEMS`; the fallback handles the
  window.
* `bad_response`: pin the last known-good prompt version under `prompts/versions/`.
* Emergency: set `AIOPS_LLM_API_KEY=""` to force deterministic mode explicitly (audited).

## Verification
1. `POST /api/v1/agents/runs/{id}/investigate` on a recent incident → diagnosis present,
   `evidence_ids` all resolve, `reasoner` field explicit.
2. `llm_error_total` stops incrementing.
3. Re-run `make eval`; diagnosis/safety scores must be at or above baseline.

## Prevention
Provider error budget alerting, prompt versioning with eval gating (EVAL-003), and a
documented fallback quality baseline so degradation is measurable rather than guessed.
