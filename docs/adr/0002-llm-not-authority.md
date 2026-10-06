# ADR-0002 — The LLM is not an authority

* **Status:** Accepted
* **Date:** 2026-10-06
* **Story:** OPS-041, OPS-061, SEC-003

## Context
The agent uses a language model for reasoning, planning and interpretation. A model can be
convinced by text it reads — and it reads logs, issue bodies, commit messages and API
responses, all of which are attacker-influenceable. Any design where the model's output
directly triggers a side effect is a design where prompt injection is remote code execution.

## Decision
The model produces **proposals**. Authority sits in deterministic components:

```text
LLM: reasoning, planning, interpretation      → NOT AUTHORITY
Deterministic system: state, policy, authorization, validation, execution, verification
```

Concretely:
* the LLM cannot name a tool that is not in the registry — unknown names are dropped and
  recorded as rejected proposals (`actions/proposals_rejected_total`);
* the LLM cannot set a role, an approval outcome, a risk level, or a severity;
* policy evaluation never calls the model; it is a pure function of (actor role, tool,
  action payload, autonomy level, incident severity);
* the verifier never reads the model's opinion of whether the fix worked — it re-queries
  the system of record.

## Consequences
* Prompt injection degrades the *quality of the diagnosis*, not the *safety of the system*.
  That is the correct blast radius, and `tests/security/prompt_injection` proves it: even a
  fully-compromised reasoner cannot produce an executed high-risk action without approval.
* The model is genuinely useful where it is unrivalled (correlating a deploy time with an
  error onset) without being trusted where it is weakest (authorization).
