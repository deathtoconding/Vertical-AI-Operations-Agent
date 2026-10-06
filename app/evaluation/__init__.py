"""Evaluation primitives (EVAL-001, EVAL-002).

The AI evaluation suite lives outside the normal test run (section 29): it grades *behaviour*
against a golden dataset rather than asserting units. This package holds the parts that are
worth having in the application itself:

* :mod:`app.evaluation.schemas` — the dataset and report contracts, validated with pydantic so
  a malformed case fails loudly instead of silently grading nothing;
* :mod:`app.evaluation.graders` — deterministic graders, one per capability the agent claims
  (detection, evidence, diagnosis, grounding, action selection, safety, verification);
* :mod:`app.evaluation.harness` — runs a case through the real detection → evidence →
  investigation → planning → policy path and collects the artefacts to grade.

Nothing here calls a live model: the harness runs the deterministic offline reasoner by default
so results are reproducible, and the *same* graders can score a live-reasoner run by pointing
``AIOPS_LLM_API_KEY`` at a real endpoint.
"""

from __future__ import annotations

from app.evaluation.graders import GRADERS, Grade
from app.evaluation.harness import Harness, run_dataset
from app.evaluation.schemas import (
    CaseResult,
    EvalCase,
    EvalReport,
    ExpectedOutcome,
    load_dataset,
)

__all__ = [
    "GRADERS",
    "CaseResult",
    "EvalCase",
    "EvalReport",
    "ExpectedOutcome",
    "Grade",
    "Harness",
    "load_dataset",
    "run_dataset",
]
