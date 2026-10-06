"""Evaluation graders, grouped by the capability they judge (EVAL-002).

The grading *logic* lives in :mod:`app.evaluation.graders` so it is importable, type-checked and
unit-tested with the rest of the application. This package is the evaluation-side view of it:
which graders apply to which dataset category, and which failure modes are hard gates rather than
score deductions.

Keeping the split explicit matters for review: changing how a *capability* is evaluated touches
``app/evaluation``, while changing what a *category* requires of the agent touches this package.
"""

from __future__ import annotations

from typing import Final

from app.evaluation.graders import (
    GRADERS,
    Grade,
    grade_action_selection,
    grade_detection,
    grade_diagnosis,
    grade_evidence,
    grade_grounding,
    grade_safety,
    grade_verification,
)

#: Graders that always apply to every case, whatever its category.
UNIVERSAL_GRADERS: Final[tuple[str, ...]] = ("safety", "grounding")

#: Category → the dimensions it stresses. The harness always runs every grader; this mapping
#: documents intent and is asserted by tests so the dataset cannot drift away from its purpose.
CATEGORY_FOCUS: Final[dict[str, tuple[str, ...]]] = {
    "normal": ("detection", "action_selection"),
    "simple_anomaly": ("detection", "evidence", "diagnosis", "action_selection", "verification"),
    "ambiguous_anomaly": ("diagnosis", "grounding", "action_selection"),
    "multiple_causes": ("diagnosis", "evidence", "grounding"),
    "false_positive": ("detection", "action_selection"),
    "tool_failure": ("action_selection", "verification"),
    "malicious_input": ("grounding", "safety", "action_selection"),
}

#: A case in one of these categories may not pass while a graded dimension is unmeasured: the
#: agent's behaviour there is exactly what the gate exists for.
NO_EMPTY_VERDICT_CATEGORIES: Final[frozenset[str]] = frozenset({"malicious_input", "tool_failure"})

__all__ = [
    "CATEGORY_FOCUS",
    "GRADERS",
    "NO_EMPTY_VERDICT_CATEGORIES",
    "UNIVERSAL_GRADERS",
    "Grade",
    "grade_action_selection",
    "grade_detection",
    "grade_diagnosis",
    "grade_evidence",
    "grade_grounding",
    "grade_safety",
    "grade_verification",
]
