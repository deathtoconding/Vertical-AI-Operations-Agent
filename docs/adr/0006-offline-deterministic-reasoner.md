# ADR-0006 — Ship a deterministic reasoner alongside the LLM, and label which one answered

* **Status:** Accepted
* **Date:** 2026-10-06
* **Story:** OPS-041, EVAL-002

## Context
An LLM-backed agent is untestable in CI without a key, and unreliable to demo without a
network. But hiding that behind fake output would violate AI coding rule 5. The system
also needs a defined behaviour when the LLM is unavailable in production (runbook:
`llm-failure.md`).

## Decision
`app/llm/provider.py` implements two reasoners behind one interface:

1. **`OpenAICompatibleLLM`** — used when `AIOPS_LLM_API_KEY` is configured. It calls a
   chat-completions endpoint, requests a JSON schema, validates the response and rejects
   ungrounded citations.
2. **`DeterministicReasoner`** — a rules-and-statistics reasoner used when no key is
   configured **or when the LLM call fails**. It ranks the same evidence with the same
   contract.

Every diagnosis carries `reasoner: "llm" | "deterministic"` and, when degraded,
`degraded_reason`. Nothing in the system pretends a deterministic result came from a model.

## Consequences
* CI exercises the full lifecycle with zero external dependencies and zero cost.
* Degradation is observable (`llm_error_rate`, `reasoner_fallback_total`) and honest, and
  the AI evaluation harness grades *both* reasoners against the same golden dataset, which
  is how we detect prompt regressions without a key.
