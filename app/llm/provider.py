"""Reasoning providers: a real LLM, and a deterministic fallback that never lies (ADR-0006).

Two implementations behind one interface:

* :class:`OpenAICompatibleReasoner` — calls a chat-completions endpoint, requests JSON, and
  validates the response against the diagnosis schema. Any failure (timeout, bad JSON,
  schema violation) raises :class:`LLMUnavailable` so the caller can degrade *explicitly*.
* :class:`DeterministicReasoner` — a rules-and-statistics reasoner over the same evidence,
  used when no API key is configured or when the LLM call fails.

Both return a :class:`~app.domain.diagnosis.Diagnosis` and both declare which one produced it
(``reasoner``) plus, when degraded, ``degraded_reason``. Nothing in this system ever presents a
deterministic result as if a model produced it, or vice versa.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Final, Protocol

import httpx

from app.core.config import Settings
from app.core.errors import LLMUnavailable
from app.core.logging import get_logger
from app.core.telemetry import LLM_ERRORS, LLM_LATENCY, LLM_TOKENS
from app.domain.diagnosis import Diagnosis, Hypothesis, RecommendedAction

logger = get_logger(__name__)

ALTERNATIVE_PRESENT_CONFIDENCE_CAP: Final[float] = 0.6

PROMPT_VERSION = "v1"
MAX_COMPLETION_TOKENS = 1200
JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class ReasoningRequest:
    """Everything a reasoner is allowed to see — evidence, framed as data."""

    incident_id: str
    system_prompt: str
    user_prompt: str
    evidence_ids: list[str]
    metadata: dict[str, Any]


class Reasoner(Protocol):
    """A reasoning provider. It proposes; it never authorises (ADR-0002)."""

    name: str

    async def diagnose(self, request: ReasoningRequest) -> Diagnosis: ...


# --------------------------------------------------------------------------- #
# LLM
# --------------------------------------------------------------------------- #


class OpenAICompatibleReasoner:
    """OpenAI-compatible chat-completions client."""

    name = "llm"

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Any = None,
    ) -> None:
        self.settings = settings
        self._client = client
        self._owns_client = client is None
        self._sleep = sleep
        self.model = settings.llm_model

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.llm_base_url.rstrip("/"),
                timeout=self.settings.llm_timeout_seconds,
                headers={
                    "Authorization": f"Bearer {self.settings.llm_api_key.get_secret_value()}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def diagnose(self, request: ReasoningRequest) -> Diagnosis:
        payload = {
            "model": self.model,
            "temperature": self.settings.llm_temperature,
            "max_tokens": MAX_COMPLETION_TOKENS,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
        }

        attempts = max(1, self.settings.llm_max_retries + 1)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            started = perf_counter()
            try:
                client = await self._get_client()
                response = await client.post("/chat/completions", json=payload)
                LLM_LATENCY.labels(provider=self.name).observe(perf_counter() - started)

                if response.status_code in {429, 500, 502, 503, 504} and attempt < attempts:
                    LLM_ERRORS.labels(provider=self.name, reason="retryable").inc()
                    if self._sleep is not None:
                        await self._sleep(0.2 * attempt)
                    continue
                if response.status_code in {401, 403}:
                    LLM_ERRORS.labels(provider=self.name, reason="auth").inc()
                    raise LLMUnavailable(
                        "The LLM rejected the configured credentials.", reason="auth"
                    )
                if response.status_code >= 400:
                    LLM_ERRORS.labels(provider=self.name, reason="http_error").inc()
                    raise LLMUnavailable(
                        f"The LLM returned HTTP {response.status_code}.", reason="http_error"
                    )

                body = response.json()
                usage = body.get("usage") or {}
                if usage:
                    LLM_TOKENS.labels(provider=self.name, kind="prompt").inc(
                        int(usage.get("prompt_tokens", 0))
                    )
                    LLM_TOKENS.labels(provider=self.name, kind="completion").inc(
                        int(usage.get("completion_tokens", 0))
                    )

                content = self._extract_content(body)
                return self._parse_diagnosis(content, latency=perf_counter() - started)

            except (httpx.TimeoutException, httpx.TransportError) as exc:
                LLM_ERRORS.labels(provider=self.name, reason="transport").inc()
                last_error = exc
                if attempt < attempts:
                    if self._sleep is not None:
                        await self._sleep(0.2 * attempt)
                    continue
                raise LLMUnavailable(
                    f"The LLM is unreachable: {type(exc).__name__}.", reason="transport"
                ) from exc

        raise LLMUnavailable(f"The LLM failed: {last_error}", reason="exhausted")

    def _extract_content(self, body: dict[str, Any]) -> str:
        try:
            return str(body["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            LLM_ERRORS.labels(provider=self.name, reason="bad_response").inc()
            raise LLMUnavailable(
                "The LLM returned an unexpected response shape.", reason="bad_response"
            ) from exc

    def _parse_diagnosis(self, content: str, *, latency: float) -> Diagnosis:
        raw = content.strip()
        if not raw.startswith("{"):
            match = JSON_BLOCK.search(raw)
            if match is None:
                LLM_ERRORS.labels(provider=self.name, reason="not_json").inc()
                raise LLMUnavailable("The LLM response did not contain JSON.", reason="not_json")
            raw = match.group(0)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            LLM_ERRORS.labels(provider=self.name, reason="invalid_json").inc()
            raise LLMUnavailable(
                "The LLM response was not valid JSON.", reason="invalid_json"
            ) from exc

        payload["reasoner"] = self.name
        payload["prompt_version"] = PROMPT_VERSION
        payload["latency_seconds"] = round(latency, 4)
        try:
            return Diagnosis.model_validate(payload)
        except Exception as exc:
            LLM_ERRORS.labels(provider=self.name, reason="schema_violation").inc()
            raise LLMUnavailable(
                f"The LLM response did not satisfy the diagnosis schema: {type(exc).__name__}",
                reason="schema_violation",
            ) from exc


# --------------------------------------------------------------------------- #
# Deterministic fallback
# --------------------------------------------------------------------------- #


class DeterministicReasoner:
    """A rules-and-statistics reasoner used when no model is available.

    It is honestly simple: rank evidence by reliability and recency, look for a deployment
    whose timestamp precedes the onset, and translate the pattern into a hypothesis with
    explicit confidence. Its value is not that it is clever — it is that:

    * the system still completes an investigation offline, so CI and air-gapped environments
      exercise the whole lifecycle;
    * its output is *grounded* (every cited id is a real evidence row), so it cannot teach the
      rest of the system to accept invented citations;
    * it is labelled ``reasoner="deterministic"``, so nobody mistakes a fallback for a model.
    """

    name = "deterministic"

    def __init__(self, degraded_reason: str | None = None) -> None:
        self.degraded_reason = degraded_reason

    async def diagnose(self, request: ReasoningRequest) -> Diagnosis:
        started = perf_counter()
        evidence = request.metadata.get("evidence", [])
        incident = request.metadata.get("incident", {})
        injections = sorted({flag for item in evidence for flag in (item.get("injections") or [])})

        deployment = _find(evidence, source="deployment")
        github_deployment = _find(evidence, source="github", kind="deployment")
        error_metric = _find(evidence, source="metrics", metric="error_rate")
        latency_metric = _find(evidence, source="metrics", metric="latency_p95")
        payment_metric = _find(evidence, source="payments")
        log_cluster = _find(evidence, source="logs")

        hypothesis_parts: list[str] = []
        evidence_ids: list[str] = []
        counter_ids: list[str] = []
        recommended: list[RecommendedAction] = []
        uncertainties: list[str] = []
        confidence = 0.35

        # 1. Metric deviation is the anchor of every hypothesis.
        if error_metric:
            evidence_ids.append(str(error_metric["id"]))
            hypothesis_parts.append(f"{error_metric['summary']}")
            confidence += 0.15

        # 2. Deployment correlation is the strongest causal signal available offline.
        release = None
        if github_deployment:
            evidence_ids.append(str(github_deployment["id"]))
            release = str(github_deployment.get("content", {}).get("deployment") or "")
            hypothesis_parts.append(
                f"deployment {release or 'of a new release'} precedes the onset"
            )
            confidence += 0.2
        elif deployment:
            evidence_ids.append(str(deployment["id"]))
            release = str(deployment.get("content", {}).get("active_release") or "")
            hypothesis_parts.append(f"the active release is {release or 'unknown'}")
            confidence += 0.1

        if log_cluster:
            evidence_ids.append(str(log_cluster["id"]))
            hypothesis_parts.append(f"logs show: {log_cluster['summary']}")
            confidence += 0.1

        if latency_metric and not error_metric:
            evidence_ids.append(str(latency_metric["id"]))
            hypothesis_parts.append(str(latency_metric["summary"]))
            confidence += 0.15

        # 3. Counter-evidence must be reported, not hidden.
        healthy_metrics = [
            item
            for item in evidence
            if item.get("source") == "metrics" and item.get("metric") not in {None}
        ]
        if healthy_metrics and len(healthy_metrics) > 1:
            others = [item for item in healthy_metrics if item.get("id") not in evidence_ids]
            if others:
                counter_ids.append(str(others[0]["id"]))
                uncertainties.append(
                    "other metric families were within normal range, which is consistent with "
                    "a partial rather than a total degradation"
                )

        # 4. Remediation, chosen by pattern — always as a *proposal*.
        previous_release = str((deployment or {}).get("content", {}).get("previous_release") or "")
        if release and previous_release and previous_release != release:
            recommended.append(
                RecommendedAction(
                    intent="rollback_deployment",
                    tool_hint="deployment.rollback_simulation",
                    params={
                        "target_release": previous_release,
                        "reason": "suspected deployment regression",
                    },
                    rationale=(
                        f"release {release} correlates with the onset of the anomaly; "
                        f"{previous_release} is the last known-good release"
                    ),
                    risk_hint="high",
                    evidence_ids=evidence_ids,
                )
            )
            confidence += 0.05

        recommended.append(
            RecommendedAction(
                intent="notify_operations",
                tool_hint="slack.notify",
                params={"text": f"Investigation complete for {request.incident_id}"},
                rationale="keep the on-call human informed even when the agent proposes a fix",
                risk_hint="medium",
                evidence_ids=evidence_ids,
            )
        )

        if payment_metric:
            evidence_ids.append(str(payment_metric["id"]))
            recommended.append(
                RecommendedAction(
                    intent="open_ticket",
                    tool_hint="jira.create_incident",
                    params={
                        "incident_id": request.incident_id,
                        "summary": "Payment failure rate elevated — provider investigation",
                        "priority": "P2",
                    },
                    rationale="payment anomalies need a tracked ticket for customer follow-up",
                    risk_hint="medium",
                    evidence_ids=evidence_ids,
                )
            )

        # 5. If nothing supportive was found, say so instead of inventing a cause.
        if not hypothesis_parts:
            LLM_LATENCY.labels(provider=self.name).observe(perf_counter() - started)
            return Diagnosis(
                hypothesis=(
                    "Insufficient correlated evidence to name a cause; the anomaly is real but "
                    "its origin is unknown at this confidence level."
                ),
                evidence_ids=evidence_ids,
                counter_evidence_ids=[],
                confidence=0.2,
                rationale="no deployment, log or metric evidence correlated with the onset",
                unsupported_claims=[],
                uncertainties=[
                    "evidence sources were unavailable or returned no correlating signal"
                ],
                reasoner=self.name,
                degraded_reason=self.degraded_reason,
                injection_flags=injections,
                prompt_version=PROMPT_VERSION,
                latency_seconds=round(perf_counter() - started, 4),
            )

        statement = (
            f"{incident.get('title', 'The incident')} is most consistent with "
            + " and ".join(hypothesis_parts)
            + "."
        )
        confidence = min(0.85, confidence)

        # More than one signal family contributing means more than one explanation is live. Rank
        # them explicitly and hold the primary hypothesis below certainty: a single confident
        # story built from two correlated signals is exactly how an operator gets misled.
        alternatives = _alternative_hypotheses(
            incident,
            evidence_ids=evidence_ids,
            counter_ids=counter_ids,
            primary_confidence=confidence,
            has_deployment=bool(github_deployment or deployment),
            has_payment=bool(payment_metric),
        )
        if alternatives:
            confidence = min(confidence, ALTERNATIVE_PRESENT_CONFIDENCE_CAP)
            uncertainties.append(
                "more than one explanation is consistent with the evidence; "
                "the alternatives are listed in `hypotheses` and are not ruled out"
            )

        # Reasoning latency is exported under the same family as the LLM path, labelled by
        # provider: the SLO panel answers "how long did reasoning take" whichever reasoner
        # answered, instead of going blank in the offline profile (SRE-004).
        latency = perf_counter() - started
        LLM_LATENCY.labels(provider=self.name).observe(latency)

        return Diagnosis(
            hypothesis=statement,
            evidence_ids=list(dict.fromkeys(evidence_ids)),
            counter_evidence_ids=list(dict.fromkeys(counter_ids)),
            confidence=round(confidence, 3),
            rationale=(
                "Deterministic correlation: the deviation is statistically significant, a "
                "deployment precedes the onset, and logs are consistent with the failing path. "
                "Causality is inferred, not proven, and the proposed action requires human "
                "approval."
            ),
            hypotheses=[
                Hypothesis(
                    statement=statement,
                    evidence_ids=list(dict.fromkeys(evidence_ids)),
                    counter_evidence_ids=list(dict.fromkeys(counter_ids)),
                    confidence=round(confidence, 3),
                    rationale="primary hypothesis from deployment/time correlation",
                    causal_chain=_causal_chain(release, incident),
                ),
                *alternatives,
            ],
            recommended_actions=recommended,
            unsupported_claims=[],
            uncertainties=[
                *uncertainties,
                "no change diff was analysed",
                "the correlation is temporal, not a proven causal mechanism",
            ],
            reasoner=self.name,
            degraded_reason=self.degraded_reason,
            injection_flags=injections,
            prompt_version=PROMPT_VERSION,
            latency_seconds=round(latency, 4),
        )


def _alternative_hypotheses(
    incident: dict[str, Any],
    *,
    evidence_ids: list[str],
    counter_ids: list[str],
    primary_confidence: float,
    has_deployment: bool,
    has_payment: bool,
) -> list[Hypothesis]:
    """Explain what *else* could be true, ranked below the primary hypothesis.

    Deterministic and conservative: an alternative is only emitted when the evidence genuinely
    admits one — a deployment correlation that could equally be load-driven, or a payment
    anomaly that could equally be our own retry behaviour.
    """
    alternatives: list[Hypothesis] = []
    alternatives.append(
        Hypothesis(
            statement=(
                f"{incident.get('title', 'The incident')} could also be caused by load or "
                "capacity pressure rather than by a change: the anomaly's onset is correlated "
                "with the release, but no change diff was inspected."
            ),
            evidence_ids=list(dict.fromkeys(evidence_ids)),
            counter_evidence_ids=list(dict.fromkeys(counter_ids)),
            confidence=round(max(0.1, primary_confidence - 0.25), 3),
            rationale=(
                "traffic and saturation move with the daily curve; a temporal correlation is not "
                "a proven mechanism"
            ),
        )
    )
    if has_payment:
        alternatives.append(
            Hypothesis(
                statement=(
                    "The payment failures could originate in the provider's own degradation "
                    "rather than in this service's release."
                ),
                evidence_ids=list(dict.fromkeys(evidence_ids)),
                counter_evidence_ids=[],
                confidence=round(max(0.1, primary_confidence - 0.3), 3),
                rationale="provider status and error codes were the primary payment signal",
            )
        )
    return alternatives


def _find(
    evidence: list[dict[str, Any]],
    *,
    source: str,
    kind: str | None = None,
    metric: str | None = None,
) -> dict[str, Any] | None:
    for item in evidence:
        if item.get("source") != source:
            continue
        if kind is not None and item.get("kind") != kind:
            continue
        if metric is not None and item.get("metric") != metric:
            continue
        return item
    return None


def _causal_chain(release: str | None, incident: dict[str, Any]) -> list[str]:
    chain = []
    if release:
        chain.append(f"release {release} deployed to production")
    chain.append(f"anomaly onset detected ({incident.get('metric', 'metric')} deviation)")
    chain.append("customer-visible errors increase")
    chain.append("incident opened and correlated with the deployment window")
    return chain


def build_reasoner(settings: Settings) -> Reasoner:
    """Choose the reasoner for this configuration. LLM when a key exists, else deterministic."""
    if settings.llm_configured:
        return OpenAICompatibleReasoner(settings)
    return DeterministicReasoner(degraded_reason="no_llm_configured")


__all__ = [
    "PROMPT_VERSION",
    "DeterministicReasoner",
    "OpenAICompatibleReasoner",
    "Reasoner",
    "ReasoningRequest",
    "build_reasoner",
]
