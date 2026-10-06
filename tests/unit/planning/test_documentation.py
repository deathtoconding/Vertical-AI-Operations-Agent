"""Documentation contract tests.

The plan, the domain vocabulary, the threat model, the SLOs and the runbooks are
deliverables. A document that drifts from the code is worse than no document, so the
parts that can be checked mechanically are checked mechanically.

Stories: OPS-001 (domain), OPS-002 (lifecycle), SEC-001 (threat model), SRE-004 (SLOs),
SRE-005 (runbooks)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.domain.enums import (
    INCIDENT_TYPE_TO_SCENARIO,
    SEVERITY_RESPONSE_TARGETS,
    AgentState,
    AuditEventType,
    EvidenceSource,
    IncidentType,
    RiskLevel,
    Role,
    VerificationOutcome,
)
from app.planning.backlog import MVP_SCENARIOS

pytestmark = pytest.mark.story("OPS-001")

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS = REPO_ROOT / "docs"


def read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Domain document <-> domain enums
# --------------------------------------------------------------------------- #


def test_domain_document_covers_every_incident_type() -> None:
    domain = read("docs/planning/domain.md")
    for incident_type in IncidentType:
        assert incident_type.value in domain, f"{incident_type.value} is not documented"


def test_domain_document_covers_every_severity() -> None:
    domain = read("docs/planning/domain.md")
    for severity in SEVERITY_RESPONSE_TARGETS:
        assert severity.value in domain


def test_domain_document_lists_every_evidence_source() -> None:
    domain = read("docs/planning/domain.md").lower()
    for source in EvidenceSource:
        assert source.value.replace("_", " ") in domain or source.value in domain, (
            f"evidence source {source.value} missing from the domain document"
        )


def test_domain_document_documents_scope_boundaries() -> None:
    domain = read("docs/planning/domain.md")
    assert "Out of scope" in domain or "out of scope" in domain
    assert "Invariants" in domain
    # Rule 7 must be visible in the domain contract, not only in a coding-rules file.
    for blocked in ("shell", "SQL", "HTTP"):
        assert blocked in domain


def test_mvp_scenarios_are_documented_and_mapped() -> None:
    domain = read("docs/planning/domain.md")
    assert set(INCIDENT_TYPE_TO_SCENARIO.values()) == set(MVP_SCENARIOS)
    for scenario_id in MVP_SCENARIOS:
        assert f"**{scenario_id}**" in domain


def test_state_machine_document_matches_enums() -> None:
    """Every state in the enum appears in the agent-runtime transition table."""
    runtime = read("docs/architecture/agent-runtime.md")
    for state in AgentState:
        assert state.value in runtime, f"state {state.value} is not documented"


# --------------------------------------------------------------------------- #
# Backlog <-> documentation
# --------------------------------------------------------------------------- #


def test_delivery_plan_lists_all_nine_sprints() -> None:
    plan = read("docs/planning/delivery-plan.md")
    for number in range(1, 10):
        assert f"| {number} |" in plan, f"sprint {number} missing from the delivery plan"


# --------------------------------------------------------------------------- #
# ADRs
# --------------------------------------------------------------------------- #

REQUIRED_ADRS = (
    "0001-modular-monolith.md",
    "0002-llm-not-authority.md",
    "0003-tool-permissions.md",
    "0004-verification-model.md",
    "0005-postgresql-system-of-record.md",
    "0006-offline-deterministic-reasoner.md",
    "0007-sandbox-integrations.md",
    "0008-static-bearer-token-auth.md",
    "0009-append-only-audit.md",
    "0010-testing-with-local-postgres.md",
)

ADR_SECTIONS = ("## Context", "## Decision", "## Consequences")


@pytest.mark.parametrize("name", REQUIRED_ADRS)
def test_adr_is_complete(name: str) -> None:
    text = (DOCS / "adr" / name).read_text(encoding="utf-8")
    assert text.startswith("# ADR-"), f"{name} must start with an ADR heading"
    assert "**Status:**" in text, f"{name} must declare a status"
    for section in ADR_SECTIONS:
        assert section in text, f"{name} is missing {section!r}"


def test_adr_numbers_are_unique_and_ordered() -> None:
    numbers = [int(path.name.split("-")[0]) for path in (DOCS / "adr").glob("*.md")]
    assert numbers == sorted(numbers)
    assert len(numbers) == len(set(numbers))


# --------------------------------------------------------------------------- #
# Threat model
# --------------------------------------------------------------------------- #

THREAT_ROW = re.compile(r"^\|\s*(T-\d{2})\s*\|(.+)\|\s*$", re.MULTILINE)


@pytest.mark.story("SEC-001")
def test_threat_model_has_rated_and_mitigated_threats() -> None:
    text = read("docs/security/threat-model.md")
    rows = THREAT_ROW.findall(text)
    assert len(rows) >= 25, "the threat model should cover the six asset classes in depth"
    ids = [row[0] for row in rows]
    assert len(ids) == len(set(ids)), "duplicate threat ids"

    for threat_id, body in rows:
        cells = [cell.strip() for cell in body.split("|")]
        assert len(cells) >= 4, f"{threat_id} row is malformed"
        rating = cells[1]
        mitigation = cells[2]
        residual = cells[3]
        assert "×" in rating, f"{threat_id} is missing a likelihood × impact rating"
        assert len(mitigation) > 15, f"{threat_id} has no real mitigation"
        assert residual, f"{threat_id} does not state its residual risk"


@pytest.mark.story("SEC-001")
def test_threat_model_covers_every_required_area() -> None:
    text = read("docs/security/threat-model.md")
    for area in ("LLM", "Tools", "External inputs", "Database", "UI", "Credentials"):
        assert area in text, f"threat model does not cover {area}"


@pytest.mark.story("SEC-001")
def test_security_controls_map_to_proof() -> None:
    controls = read("docs/security/security-controls.md")
    for control in (
        "SAST",
        "Dependency scanning",
        "Secret scanning",
        "Container scanning",
        "SBOM",
        "RBAC",
        "Least privilege",
        "Audit logging",
        "Threat modeling",
        "Prompt-injection tests",
        "Input validation",
        "Rate limiting",
        "Dependency pinning",
    ):
        assert control in controls, f"control {control!r} missing from the controls table"


# --------------------------------------------------------------------------- #
# SLOs
# --------------------------------------------------------------------------- #


def test_slos_document_objectives_and_error_budget() -> None:
    text = read("docs/sre/slos.md")
    for target in ("99.5%", "60 s", "120 s", "30 s", "99%", "0"):
        assert target in text, f"missing SLO target {target}"
    assert "initial project targets" in text, "targets must be labelled as not-yet-measured"
    assert "Error budget" in text or "error budget" in text


def test_slos_declare_the_zero_tolerance_objectives() -> None:
    text = read("docs/sre/slos.md")
    assert "Lost critical incidents" in text
    assert "Unauthorized high-risk actions" in text
    assert "not error budgets" in text


# --------------------------------------------------------------------------- #
# Runbooks
# --------------------------------------------------------------------------- #

REQUIRED_RUNBOOKS = {
    "database-failure.md": ["Symptoms", "Impact", "Diagnosis", "Mitigation", "Verification"],
    "llm-failure.md": ["Symptoms", "Impact", "Diagnosis", "Mitigation", "Verification"],
    "github-unavailable.md": ["Symptoms", "Impact", "Diagnosis", "Mitigation", "Verification"],
    "jira-unavailable.md": ["Symptoms", "Impact", "Diagnosis", "Mitigation", "Verification"],
    "excessive-agent-errors.md": [
        "Symptoms",
        "Impact",
        "Diagnosis",
        "Mitigation",
        "Verification",
    ],
    "unsafe-agent-behaviour.md": ["Symptoms", "Impact", "Diagnosis", "Mitigation", "Verification"],
}


@pytest.mark.parametrize(("name", "sections"), sorted(REQUIRED_RUNBOOKS.items()))
@pytest.mark.story("SRE-005")
def test_runbook_has_required_sections(name: str, sections: list[str]) -> None:
    text = (DOCS / "sre" / "runbooks" / name).read_text(encoding="utf-8")
    for section in sections:
        # Symptoms may be declared as "## Immediate containment" style variants; require the
        # canonical headings for the five core sections.
        assert section in text or section.lower() in text.lower(), f"{name}: missing {section}"


@pytest.mark.story("SRE-005")
def test_runbooks_reference_real_metrics_and_endpoints() -> None:
    """Runbooks must point at things that exist, not at invented ones."""
    from app.core.telemetry import METRIC_NAMES  # imported lazily: telemetry ships with SRE-001

    known_metrics = set(METRIC_NAMES)
    for name in REQUIRED_RUNBOOKS:
        text = (DOCS / "sre" / "runbooks" / name).read_text(encoding="utf-8")
        for metric in re.findall(r"`(aiops_[a-z0-9_]+)", text):
            assert metric in known_metrics, f"{name} references unknown metric {metric}"


@pytest.mark.story("SRE-005")
def test_unsafe_behaviour_runbook_has_containment_first() -> None:
    text = read("docs/sre/runbooks/unsafe-agent-behaviour.md")
    assert "observe_only" in text, "the containment step must disable autonomy"
    assert "/api/v1/admin/autonomy" in text
    assert "Preserve evidence" in text


# --------------------------------------------------------------------------- #
# Coding rules & audit vocabulary
# --------------------------------------------------------------------------- #


def test_ai_coding_rules_are_documented_and_numbered() -> None:
    text = read("docs/planning/ai-coding-rules.md")
    for rule in range(1, 10):
        assert f"## Rule {rule} " in text, f"AI coding rule {rule} is missing"


def test_role_and_risk_vocabulary_is_documented() -> None:
    domain = read("docs/planning/domain.md")
    for role in Role:
        assert role.value in domain
    for risk in RiskLevel:
        assert risk.value.lower() in domain.lower()


def test_verification_outcomes_documented() -> None:
    verification = read("docs/architecture/verification.md")
    for outcome in VerificationOutcome:
        assert outcome.value in verification


def test_audit_event_types_are_reachable_from_documentation() -> None:
    """Every audited event type must be mentioned by at least one document."""
    corpus = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [
            DOCS / "security" / "security-controls.md",
            DOCS / "architecture" / "agent-runtime.md",
            DOCS / "architecture" / "verification.md",
            DOCS / "planning" / "domain.md",
            DOCS / "architecture" / "audit-trail.md",
        ]
    ).lower()
    undocumented = [
        event.value
        for event in AuditEventType
        if event.value not in corpus and event.value.replace("_", " ") not in corpus
    ]
    # Security events are allowed to live only in the controls document, but they must be
    # named *somewhere*.
    assert not undocumented, f"audit events with no documentation: {undocumented}"
