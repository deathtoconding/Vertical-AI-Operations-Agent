# ADR-0008 — Static bearer tokens with role binding for the MVP

* **Status:** Accepted (revisit before multi-tenant)
* **Date:** 2026-10-06
* **Story:** SEC-002

## Context
RBAC must be real (roles, least privilege, audited denials) but an identity provider is a
large dependency for a single-tenant MVP.

## Decision
Authenticate with static bearer tokens supplied via `AIOPS_API_TOKENS`
(`token:role:actor_id`), validated with a constant-time comparison, and bound to exactly
one role from `viewer | operator | sre | admin`. Authorization is deny-by-default and
checked per route *and* per tool execution.

## Consequences
* Real RBAC semantics (401 vs 403, audited denials, tool permission matrix) are implemented
  and tested now; swapping in OIDC later changes token validation only.
* Tokens are never logged (redaction processor) and are supplied from the environment or a
  secret manager, never source (rule 6).
* Documented limitation: no token rotation, no expiry, no MFA — recorded as residual risk
  in `docs/security/threat-model.md`.
