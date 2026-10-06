"""LLM layer tests (OPS-041, SEC-003, ADR-0006).

Two halves: the *prompt artefacts* (they are deployment inputs, not strings) and the
*deterministic reasoner* (which must behave like a reasoner — grounded, honest about
confidence — rather than a stub that fakes success).
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.errors import LLMUnavailable
from app.llm.prompts import PromptNotFound, available_prompts, load_prompt, prompt_version
from app.llm.provider import (
    DeterministicReasoner,
    OpenAICompatibleReasoner,
    ReasoningRequest,
    build_reasoner,
)

pytestmark = [pytest.mark.story("OPS-041"), pytest.mark.unit]


def request_with(evidence: list[dict], incident_id: str = "INC-1") -> ReasoningRequest:
    return ReasoningRequest(
        incident_id=incident_id,
        system_prompt=load_prompt("investigation"),
        user_prompt="EVIDENCE\n(see metadata)",
        evidence_ids=[item["id"] for item in evidence],
        metadata={
            "evidence": evidence,
            "evidence_ids": [item["id"] for item in evidence],
            "anomaly": {"metric": "error_rate"},
            "incident": {"id": incident_id, "metric": "error_rate"},
        },
    )


EVIDENCE = [
    {
        "id": "EV-AAAA",
        "source": "metrics",
        "kind": "metric_series",
        "summary": "error_rate rose from 0.0126 to 0.19",
        "content": {"metric": "error_rate", "latest": 0.19, "earliest": 0.0126},
    },
    {
        "id": "EV-BBBB",
        "source": "deployment",
        "kind": "deployment",
        "summary": "active release release-42 (previous release-41)",
        "content": {"active_release": "release-42", "previous_release": "release-41"},
    },
    {
        "id": "EV-CCCC",
        "source": "github",
        "kind": "commit",
        "summary": "commit abc123 by dev: fix checkout retry logic",
        "content": {"sha": "abc123", "deployment": {"release": "release-42"}},
    },
    {
        "id": "EV-DDDD",
        "source": "logs",
        "kind": "log_cluster",
        "summary": "20 log lines, 16 at WARN/ERROR",
        "content": {"error_lines": 16, "injections": ["instruction_override"]},
    },
]


# ------------------------------------------------------------------- prompts


def test_required_prompt_artefacts_exist() -> None:
    names = available_prompts()
    assert "investigation" in names


def test_prompt_contains_the_untrusted_data_contract() -> None:
    prompt = load_prompt("investigation")
    for phrase in ("UNTRUSTED_DATA", "JSON only", "evidence_ids", "confidence"):
        assert phrase in prompt, phrase
    assert "never" in prompt.lower()


def test_missing_prompt_is_loud() -> None:
    with pytest.raises(PromptNotFound):
        load_prompt("no-such-prompt")


def test_prompt_version_is_derived_from_content() -> None:
    version = prompt_version("investigation")
    assert len(version) == 12
    assert all(character in "0123456789abcdef" for character in version)
    # Same file, same version: a version that changes without an edit is not a version.
    assert version == prompt_version("investigation")


# ------------------------------------------------------------------ selection


def test_reasoner_selection_follows_configuration() -> None:
    offline = Settings(env="test", llm_api_key="")
    assert isinstance(build_reasoner(offline), DeterministicReasoner)

    configured = Settings(env="test", llm_api_key="sk-placeholder")
    reasoner = build_reasoner(configured)
    assert isinstance(reasoner, OpenAICompatibleReasoner)
    assert reasoner.name == "llm"


# --------------------------------------------------------- deterministic reasoner


async def test_deterministic_reasoner_is_grounded_in_real_evidence_ids() -> None:
    reasoner = DeterministicReasoner()
    diagnosis = await reasoner.diagnose(request_with(EVIDENCE))
    known = {item["id"] for item in EVIDENCE}
    assert diagnosis.reasoner == "deterministic"
    assert diagnosis.evidence_ids
    assert set(diagnosis.evidence_ids) <= known
    assert set(diagnosis.counter_evidence_ids) <= known
    assert diagnosis.confidence < 0.8  # it does not oversell correlation as proof


async def test_deterministic_reasoner_proposes_bounded_rollback_when_deployment_is_suspect() -> (
    None
):
    reasoner = DeterministicReasoner()
    diagnosis = await reasoner.diagnose(request_with(EVIDENCE))
    rollback = [
        item for item in diagnosis.recommended_actions if item.intent == "rollback_deployment"
    ]
    assert rollback, "a deployment-correlated error spike should propose a rollback"
    assert rollback[0].tool_hint == "deployment.rollback_simulation"
    assert rollback[0].params["target_release"] == "release-41"
    assert rollback[0].risk_hint == "high"


async def test_deterministic_reasoner_never_proposes_an_unregistered_tool() -> None:
    from app.tools.registry import build_default_registry

    registry = build_default_registry()
    reasoner = DeterministicReasoner()
    diagnosis = await reasoner.diagnose(request_with(EVIDENCE))
    for action in diagnosis.recommended_actions:
        assert action.tool_hint in registry.names(), action.tool_hint


async def test_deterministic_reasoner_marks_its_degradation() -> None:
    reasoner = DeterministicReasoner(degraded_reason="llm_unavailable")
    diagnosis = await reasoner.diagnose(request_with(EVIDENCE))
    assert diagnosis.degraded_reason == "llm_unavailable"
    assert diagnosis.is_degraded


async def test_deterministic_reasoner_states_uncertainty_with_thin_evidence() -> None:
    reasoner = DeterministicReasoner()
    diagnosis = await reasoner.diagnose(request_with(EVIDENCE[:1]))
    assert diagnosis.confidence <= 0.6
    assert diagnosis.uncertainties


# --------------------------------------------------------- LLM client contract


async def test_llm_client_reports_unavailability_instead_of_fabricating(settings: Settings) -> None:
    """With no API key the client must raise a typed failure — never invent a diagnosis."""
    offline = Settings(env="test", llm_base_url="https://llm.invalid/v1", llm_api_key="")
    client = OpenAICompatibleReasoner(offline)
    with pytest.raises(LLMUnavailable):
        await client.diagnose(request_with(EVIDENCE))
