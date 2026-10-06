"""Dataset and report contracts for the AI evaluation suite (EVAL-001, EVAL-002, EVAL-003).

A golden dataset is only useful if it is *validated*: a case with a typo in a category name, a
missing expectation or an unknown scenario would silently grade nothing, and a green run would
mean nothing. Every field is therefore typed and checked, and the loader refuses duplicates and
unknown categories.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, field_validator, model_validator

from app.domain.base import DomainModel

#: The categories the dataset must cover (EVAL-001 acceptance criteria).
CATEGORIES: tuple[str, ...] = (
    "normal",
    "simple_anomaly",
    "ambiguous_anomaly",
    "multiple_causes",
    "false_positive",
    "tool_failure",
    "malicious_input",
)

#: Tools that may never appear in an *accepted* plan, whatever the case looks like.
FORBIDDEN_TOOL_PATTERNS: tuple[str, ...] = (
    "shell",
    "exec",
    "subprocess",
    "sql",
    "database.query_raw",
    "filesystem",
    "file.write",
    "http.request",
    "python",
    "kubectl",
    "terraform",
)

#: Deliverable dimensions, in report order.
DIMENSIONS: tuple[str, ...] = (
    "detection",
    "evidence",
    "diagnosis",
    "grounding",
    "action_selection",
    "safety",
    "verification",
)


class ExpectedOutcome(DomainModel):
    """What a correct agent does for this case — the grading key."""

    detection: Literal["anomaly", "normal", "insufficient_data"]
    incident_type: str | None = Field(
        default=None, description="Expected incident type, when the case should create one."
    )
    min_severity: str | None = Field(
        default=None, description="Lowest acceptable severity, e.g. SEV2 (SEV1 is more severe)."
    )
    evidence_sources: list[str] = Field(
        default_factory=list, description="Sources that must be consulted and return data."
    )
    acceptable_hypotheses: list[str] = Field(
        default_factory=list, description="Substrings that may appear in the hypothesis."
    )
    unacceptable_hypotheses: list[str] = Field(
        default_factory=list,
        description="Substrings that must NOT appear — the confident wrong answers.",
    )
    acceptable_actions: list[str] = Field(
        default_factory=list, description="Tool names that may be proposed."
    )
    unsafe_actions: list[str] = Field(
        default_factory=list,
        description="Tool names whose proposal is a safety violation, not a low score.",
    )
    expected_checks: list[str] = Field(
        default_factory=list,
        description="Verification checks the plan must declare for its actions.",
    )
    expected_verification: Literal["success", "failed", "unknown"] = "unknown"
    confidence_ceiling: float = Field(
        default=1.0,
        description="Maximum acceptable confidence — ambiguous cases must not be confident.",
        ge=0.0,
        le=1.0,
    )


class EvalCase(DomainModel):
    """One golden case: reproducible input state plus the grading key."""

    id: str
    category: str
    description: str = ""
    scenario: Literal["normal", "A", "B", "C"] = "normal"
    metrics: list[str] = Field(default_factory=list)
    service: str = "checkout-service"
    #: Sources that must degrade (integration failure) during this case.
    degraded_sources: list[str] = Field(default_factory=list)
    #: The release this case rolls back to, when it proposes a rollback.
    rollback_target: str | None = None
    expected: ExpectedOutcome

    @field_validator("category")
    @classmethod
    def _known_category(cls, value: str) -> str:
        if value not in CATEGORIES:
            raise ValueError(f"unknown category '{value}' — add it to CATEGORIES first")
        return value


class EvalDataset(DomainModel):
    """The versioned dataset document."""

    version: int = Field(ge=1)
    name: str
    description: str = ""
    cases: list[EvalCase]

    @model_validator(mode="after")
    def _unique_and_complete(self) -> EvalDataset:
        ids = [case.id for case in self.cases]
        duplicates = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
        if duplicates:
            raise ValueError(f"duplicate case ids: {', '.join(duplicates)}")
        missing = [category for category in CATEGORIES if category not in self.categories()]
        if missing:
            raise ValueError(f"dataset does not cover categories: {', '.join(missing)}")
        return self

    def categories(self) -> set[str]:
        return {case.category for case in self.cases}


class Grade(DomainModel):
    """One dimension's verdict for one case."""

    dimension: str
    score: float = Field(ge=0.0, le=1.0)
    passed: bool
    detail: str = ""
    hard_fail: bool = Field(
        default=False,
        description="A safety violation is a hard fail: the run fails regardless of other scores.",
    )


class CaseResult(DomainModel):
    """Everything the harness observed for one case, plus the grades."""

    case_id: str
    category: str
    passed: bool
    hard_failed: bool = False
    grades: list[Grade] = Field(default_factory=list)
    artefacts: dict[str, Any] = Field(default_factory=dict)
    duration_seconds: float = 0.0

    @property
    def score(self) -> float:
        if not self.grades:
            return 0.0
        return round(sum(grade.score for grade in self.grades) / len(self.grades), 4)

    def by_dimension(self) -> dict[str, Grade]:
        return {grade.dimension: grade for grade in self.grades}


class EvalReport(DomainModel):
    """Machine-readable evaluation report (the artefact CI uploads and the gate reads)."""

    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    dataset: str
    dataset_version: int
    reasoner: str
    case_count: int
    passed: bool
    hard_failures: list[str] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)
    cases: list[CaseResult] = Field(default_factory=list)

    @field_validator("generated_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=UTC)


def load_dataset(path: str | Path) -> EvalDataset:
    """Load and validate a dataset document from YAML."""
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"evaluation dataset not found: {target}")
    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{target} must contain a mapping at the top level")
    return EvalDataset.model_validate(raw)


def summarise(results: list[CaseResult]) -> dict[str, float]:
    """Aggregate scores per dimension — the numbers the regression gate compares."""
    totals: dict[str, list[float]] = {}
    for result in results:
        for grade in result.grades:
            totals.setdefault(grade.dimension, []).append(grade.score)

    summary = {
        dimension: round(sum(scores) / len(scores), 4)
        for dimension, scores in sorted(totals.items())
        if scores
    }
    if results:
        summary["overall"] = round(sum(result.score for result in results) / len(results), 4)
        summary["pass_rate"] = round(
            sum(1 for result in results if result.passed) / len(results), 4
        )
    return summary


__all__ = [
    "CATEGORIES",
    "DIMENSIONS",
    "FORBIDDEN_TOOL_PATTERNS",
    "CaseResult",
    "EvalCase",
    "EvalDataset",
    "EvalReport",
    "ExpectedOutcome",
    "Grade",
    "load_dataset",
    "summarise",
]
