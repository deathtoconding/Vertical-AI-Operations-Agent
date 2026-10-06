"""Prompt context assembly (OPS-041 / SEC-003).

The model never sees raw system output. It sees:

1. a bounded, ranked, normalised evidence set;
2. each item framed inside an explicit ``UNTRUSTED_DATA`` block that states what it is;
3. an instruction to report attempted instructions instead of following them;
4. nothing else — no credentials, no customer identities, no unrestricted tool list.

Ranking is by reliability and recency, with injection-flagged evidence kept but demoted, so an
attacker cannot flood the context with their own content and push the real signal out. The
total size is bounded by ``AIOPS_LLM_MAX_PROMPT_CHARS``, because an unbounded prompt is both a
cost incident and an injection opportunity.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from app.core.sanitization import frame_untrusted
from app.domain.evidence import Evidence
from app.domain.incidents import Incident

PROMPT_VERSION: Final[str] = "v1"

#: Evidence selected for the prompt, in priority order. The agent decides *what* matters;
#: the collector decides *what exists*.
MAX_EVIDENCE_ITEMS: Final[int] = 24


def rank_evidence(items: list[Evidence]) -> list[Evidence]:
    """Reliability and recency first; flagged evidence last but never dropped."""
    return sorted(items, key=lambda item: item.rank_key(), reverse=True)


def select_evidence(items: list[Evidence], limit: int = MAX_EVIDENCE_ITEMS) -> list[Evidence]:
    ranked = rank_evidence(items)
    return ranked[:limit]


def render_evidence(items: list[Evidence]) -> str:
    blocks: list[str] = []
    for item in items:
        flags = f" [FLAGGED: {', '.join(item.injections)}]" if item.injections else ""
        blocks.append(
            frame_untrusted(
                f"{item.source.value}/{item.kind.value} id={item.id} "
                f"at={item.timestamp.isoformat()} confidence={item.confidence.value}{flags}",
                f"{item.summary}\n{_render_content(item.content)}",
            )
        )
    return "\n\n".join(blocks) if blocks else "(no evidence was available)"


def _render_content(content: dict[str, Any]) -> str:
    if not content:
        return ""
    lines: list[str] = []
    for key, value in content.items():
        if key in {"lines", "customers", "injections"}:
            continue
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def build_investigation_prompt(
    incident: Incident,
    evidence: list[Evidence],
    *,
    anomaly: dict[str, Any] | None = None,
    degradations: list[dict[str, str]] | None = None,
    max_chars: int = 24_000,
) -> tuple[str, str, dict[str, Any]]:
    """Return ``(system_prompt, user_prompt, metadata)``.

    The metadata is what the deterministic fallback reasons over — the same structured facts,
    without the prose. Both reasoners therefore see one representation of the world.
    """
    from app.llm.prompts import load_prompt

    system_prompt = load_prompt("investigation")
    selected = select_evidence(evidence)

    degradation_note = (
        "\n".join(
            f"- {item.get('source')}: {item.get('reason')} ({item.get('detail', '')})"
            for item in (degradations or [])
        )
        or "- none: every planned source answered"
    )
    flagged = sorted({flag for item in evidence for flag in item.injections})

    user_prompt = f"""INCIDENT
  id: {incident.id}
  type: {incident.incident_type.value}
  severity: {incident.severity.value}
  service: {incident.service}
  metric: {incident.metric}
  detected_at: {incident.detected_at.isoformat()}
  title: {incident.title}
  summary: {incident.summary}

DETECTION (deterministic statistics, not model output)
{_render_detection(anomaly)}

EVIDENCE SOURCE DEGRADATIONS
{degradation_note}

INJECTION FLAGS OBSERVED IN UNTRUSTED CONTENT
{", ".join(flagged) if flagged else "none"}

EVIDENCE ({len(selected)} of {len(evidence)} items, ranked by reliability)
{render_evidence(selected)}

Respond with JSON only, matching the required schema exactly.
"""
    if len(user_prompt) > max_chars:
        user_prompt = user_prompt[:max_chars] + "\n…[prompt truncated to protect the model window]"

    metadata: dict[str, Any] = {
        "evidence": [
            {
                "id": item.id,
                "source": item.source.value,
                "kind": item.kind.value,
                "summary": item.summary,
                "metric": item.content.get("metric"),
                "content": {
                    key: value for key, value in item.content.items() if key not in {"lines"}
                },
                "timestamp": item.timestamp.isoformat(),
                "confidence": item.confidence.value,
                "injections": item.injections,
            }
            for item in selected
        ],
        "evidence_ids": [item.id for item in selected],
        "anomaly": anomaly or {},
        "incident": {
            "id": incident.id,
            "title": incident.title,
            "metric": incident.metric,
            "service": incident.service,
            "severity": incident.severity.value,
            "incident_type": incident.incident_type.value,
        },
        "degradations": degradations or [],
        "injection_flags": flagged,
        "prompt_version": PROMPT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    return system_prompt, user_prompt, metadata


def _render_detection(anomaly: dict[str, Any] | None) -> str:
    if not anomaly:
        return "  (no detection record attached)"
    lines = [f"  {key}: {value}" for key, value in anomaly.items() if key != "thresholds"]
    thresholds = anomaly.get("thresholds") or {}
    if thresholds:
        lines.append(f"  thresholds: {thresholds}")
    return "\n".join(lines)


__all__ = [
    "MAX_EVIDENCE_ITEMS",
    "PROMPT_VERSION",
    "build_investigation_prompt",
    "rank_evidence",
    "render_evidence",
    "select_evidence",
]
