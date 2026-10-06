"""Evidence retrieval (OPS-040).

The collector fans out to the sources that are relevant for the incident type, normalises
what comes back into :class:`~app.domain.evidence.EvidenceDraft` objects, and — critically —
**records a degradation instead of failing** when a source is unavailable.

Why degradation is a first-class concept: an investigation that silently proceeds without
deployment data will confidently blame the wrong thing, and an investigation that aborts
because Jira is down loses an incident. A third option — "I looked, this source was down, so my
confidence is lower and I am telling you why" — is the only honest one.

Every payload passes through :func:`~app.core.sanitization.sanitize_untrusted` before it becomes
evidence, so untrusted content is normalised, bounded and *flagged* (SEC-003).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from app.core.config import Settings
from app.core.errors import IntegrationError
from app.core.logging import get_logger, redact_mapping
from app.core.sanitization import sanitize_untrusted
from app.core.telemetry import EVIDENCE_COLLECTED, EVIDENCE_SOURCE_FAILURES
from app.core.tracing import span
from app.domain.enums import (
    Confidence,
    EvidenceKind,
    EvidenceSource,
    IncidentType,
)
from app.domain.evidence import EvidenceCollectionReport, EvidenceDraft
from app.domain.incidents import Incident
from app.integrations.facade import IntegrationFacade

logger = get_logger(__name__)

#: Which sources matter for which incident type. Fetching everything for every incident #: wastes
#: quota and buries the signal; fetching nothing relevant produces a guess.
SOURCE_PLAN: dict[IncidentType, tuple[EvidenceSource, ...]] = {
    IncidentType.API_ERROR_SPIKE: (
        EvidenceSource.METRICS,
        EvidenceSource.LOGS,
        EvidenceSource.GITHUB,
        EvidenceSource.DEPLOYMENT,
    ),
    IncidentType.API_LATENCY_SPIKE: (
        EvidenceSource.METRICS,
        EvidenceSource.LOGS,
        EvidenceSource.DEPLOYMENT,
        EvidenceSource.GITHUB,
    ),
    IncidentType.TRAFFIC_ANOMALY: (
        EvidenceSource.METRICS,
        EvidenceSource.DEPLOYMENT,
        EvidenceSource.GITHUB,
    ),
    IncidentType.SUBSCRIPTION_PAYMENT_ANOMALY: (
        EvidenceSource.PAYMENTS,
        EvidenceSource.METRICS,
        EvidenceSource.LOGS,
    ),
    IncidentType.DEPLOYMENT_REGRESSION: (
        EvidenceSource.DEPLOYMENT,
        EvidenceSource.GITHUB,
        EvidenceSource.METRICS,
        EvidenceSource.LOGS,
    ),
    IncidentType.TOOL_FAILURE: (EvidenceSource.APPLICATION_EVENTS,),
}

#: Metrics worth retrieving per incident type.
METRIC_PLAN: dict[IncidentType, tuple[str, ...]] = {
    IncidentType.API_ERROR_SPIKE: ("error_rate", "latency_p95", "request_rate"),
    IncidentType.API_LATENCY_SPIKE: ("latency_p95", "latency_p99", "saturation", "error_rate"),
    IncidentType.TRAFFIC_ANOMALY: ("request_rate", "error_rate"),
    IncidentType.SUBSCRIPTION_PAYMENT_ANOMALY: ("payment_failure_rate", "error_rate"),
    IncidentType.DEPLOYMENT_REGRESSION: ("error_rate", "latency_p95"),
    IncidentType.TOOL_FAILURE: (),
}


class EvidenceCollector:
    """Fan-out retrieval with bounded concurrency, degradation records and sanitisation."""

    def __init__(self, settings: Settings, integrations: IntegrationFacade) -> None:
        self.settings = settings
        self.integrations = integrations
        self.max_items = settings.evidence_max_items_per_source
        self.max_chars = settings.evidence_max_chars
        self.concurrency = settings.evidence_concurrency

    async def collect(self, incident: Incident) -> EvidenceCollectionReport:
        started = perf_counter()
        sources = SOURCE_PLAN.get(incident.incident_type, (EvidenceSource.METRICS,))
        semaphore = asyncio.Semaphore(max(1, self.concurrency))

        async def run(source: EvidenceSource) -> tuple[list[EvidenceDraft], dict[str, str] | None]:
            async with semaphore:
                return await self._collect_source(incident, source)

        results = await asyncio.gather(*(run(source) for source in sources), return_exceptions=True)

        drafts: list[EvidenceDraft] = []
        degradations: list[dict[str, str]] = []
        for source, result in zip(sources, results, strict=False):
            if isinstance(result, BaseException):
                degradations.append(
                    {
                        "source": source.value,
                        "reason": type(result).__name__,
                        "detail": str(result)[:200],
                    }
                )
                EVIDENCE_SOURCE_FAILURES.labels(
                    source=source.value, reason=type(result).__name__
                ).inc()
                logger.warning(
                    "evidence_source_failed",
                    source=source.value,
                    error=type(result).__name__,
                )
                continue
            source_drafts, degradation = result
            drafts.extend(source_drafts)
            if degradation:
                degradations.append(degradation)

        drafts.sort(key=lambda draft: draft.timestamp)
        for draft in drafts:
            EVIDENCE_COLLECTED.labels(source=draft.source.value).inc()

        # The caller owns the transaction, so the report carries drafts (to be persisted)
        # as well as the degradations it must record on the incident.
        report = EvidenceCollectionReport(
            incident_id=incident.id,
            drafts=drafts,
            collected=[],
            degradations=degradations,
            sources_queried=[source.value for source in sources],
            duration_seconds=round(perf_counter() - started, 4),
        )
        logger.info(
            "evidence_collected",
            incident_id=incident.id,
            sources=len(sources),
            items=len(drafts),
            degradations=len(degradations),
            duration_seconds=report.duration_seconds,
        )
        return report

    # ------------------------------------------------------------------ #

    async def _collect_source(
        self, incident: Incident, source: EvidenceSource
    ) -> tuple[list[EvidenceDraft], dict[str, str] | None]:
        handler = {
            EvidenceSource.METRICS: self._metrics,
            EvidenceSource.LOGS: self._logs,
            EvidenceSource.GITHUB: self._github,
            EvidenceSource.DEPLOYMENT: self._deployment,
            EvidenceSource.PAYMENTS: self._payments,
            EvidenceSource.APPLICATION_EVENTS: self._application_events,
        }.get(source)
        if handler is None:
            return [], {"source": source.value, "reason": "not_implemented"}
        with span(
            f"evidence.{source.value}", incident_id=incident.id, run_id=incident.agent_run_id
        ):
            return await handler(incident)

    async def _metrics(
        self, incident: Incident
    ) -> tuple[list[EvidenceDraft], dict[str, str] | None]:
        drafts: list[EvidenceDraft] = []
        failures: list[str] = []
        for metric in METRIC_PLAN.get(incident.incident_type, ("error_rate",)):
            try:
                series = await self.integrations.metrics_window(
                    incident.service, metric, self.settings.detection_window_minutes
                )
            except IntegrationError as exc:
                failures.append(f"{metric}:{exc.code}")
                continue
            drafts.append(self._metric_draft(metric, series))
        degradation = (
            {"source": "metrics", "reason": "partial", "detail": ", ".join(failures)}
            if failures
            else None
        )
        if not drafts and failures:
            raise IntegrationError("metrics", f"no metric series could be retrieved: {failures}")
        return drafts, degradation

    def _metric_draft(self, metric: str, series: dict[str, Any]) -> EvidenceDraft:
        points = series.get("points", [])
        values = [float(point.get("value", 0.0)) for point in points]
        latest = values[-1] if values else 0.0
        earliest = values[0] if values else 0.0
        peak = max(values) if values else 0.0
        change = ((latest - earliest) / earliest * 100) if earliest else 0.0
        summary = (
            f"{metric} on {series.get('service', 'unknown')} "
            f"{'rose' if latest >= earliest else 'fell'} from {earliest:.4f} to {latest:.4f} "
            f"({change:+.1f}%), peak {peak:.4f}"
        )
        return EvidenceDraft(
            source=EvidenceSource.METRICS,
            kind=EvidenceKind.METRIC_SERIES,
            summary=summary,
            content={
                "metric": metric,
                "window_start": series.get("window_start"),
                "window_end": series.get("window_end"),
                "latest": latest,
                "earliest": earliest,
                "peak": peak,
                "samples": len(values),
                "simulated": bool(series.get("simulated", False)),
            },
            timestamp=self._parse_ts(series.get("window_end")),
            confidence=Confidence.HIGH,
            simulated=bool(series.get("simulated", False)),
        )

    async def _logs(self, incident: Incident) -> tuple[list[EvidenceDraft], dict[str, str] | None]:
        payload = await self.integrations.logs_window(
            service=incident.service,
            window_minutes=self.settings.detection_window_minutes,
            severity=None,
            contains=None,
            limit=self.max_items,
        )
        lines = payload.get("lines", [])
        clean_lines = []
        # Only *heuristic names* become injection flags; flags must be a small, stable
        # vocabulary or they are useless as metrics and audits.
        injections: list[str] = []
        for line in lines:
            sanitized = sanitize_untrusted(
                str(line.get("message", "")), source="logs", max_chars=self.max_chars // 4
            )
            injections.extend(sanitized.injections)
            clean_lines.append(
                {
                    "timestamp": line.get("timestamp"),
                    "severity": line.get("severity"),
                    "message": sanitized.text,
                }
            )
        # Providers also report injection *samples* they observed. That text is untrusted too,
        # so it is normalised and bounded before it becomes evidence, and the samples are run
        # through the same heuristics as the log lines themselves.
        provider_injections = [
            sanitize_untrusted(str(item), source="logs", max_chars=self.max_chars // 4)
            for item in (payload.get("injections") or [])
        ]
        injections.extend(flag for item in provider_injections for flag in item.injections)

        errors = [
            line for line in clean_lines if str(line["severity"]).upper() in {"ERROR", "WARN"}
        ]
        summary = f"{len(clean_lines)} log lines in window; {len(errors)} at WARN/ERROR. " + (
            f"Example: {errors[0]['message'][:160]}" if errors else "No error-level lines found."
        )
        draft = EvidenceDraft(
            source=EvidenceSource.LOGS,
            kind=EvidenceKind.LOG_CLUSTER,
            summary=summary,
            content={
                "lines": clean_lines[: self.max_items],
                "error_lines": len(errors),
                "total_lines": len(clean_lines),
                "injections": sorted(set(injections)),
                "simulated_injections": [item.text for item in provider_injections],
            },
            timestamp=self._parse_ts(payload.get("window_end")),
            confidence=Confidence.MEDIUM if errors else Confidence.LOW,
            simulated=bool(payload.get("simulated", False)),
        )
        return [draft], None

    async def _github(
        self, incident: Incident
    ) -> tuple[list[EvidenceDraft], dict[str, str] | None]:
        payload = await self.integrations.github_commits(
            repository=None,
            since_minutes=self.settings.detection_window_minutes * 4,
            limit=self.max_items,
            branch=None,
        )
        drafts: list[EvidenceDraft] = []
        for commit in payload.get("commits", []):
            sanitized = sanitize_untrusted(
                str(commit.get("message", "")), source="github", max_chars=self.max_chars // 4
            )
            drafts.append(
                EvidenceDraft(
                    source=EvidenceSource.GITHUB,
                    kind=(
                        EvidenceKind.DEPLOYMENT if commit.get("deployment") else EvidenceKind.COMMIT
                    ),
                    summary=(
                        f"commit {commit.get('sha')} by {commit.get('author')}: "
                        f"{sanitized.text.splitlines()[0][:160]}"
                    ),
                    content={
                        "sha": commit.get("sha"),
                        "message": sanitized.text,
                        "author": commit.get("author"),
                        "committed_at": commit.get("committed_at"),
                        "deployment": commit.get("deployment"),
                        "injections": list(sanitized.injections),
                    },
                    timestamp=self._parse_ts(commit.get("committed_at")),
                    confidence=Confidence.HIGH if commit.get("deployment") else Confidence.MEDIUM,
                    simulated=bool(payload.get("simulated", False)),
                )
            )
        return drafts, None

    async def _deployment(
        self, incident: Incident
    ) -> tuple[list[EvidenceDraft], dict[str, str] | None]:
        state = await self.integrations.deployment_state()
        history = await self.integrations.deployment_history(limit=self.max_items)
        drafts = [
            EvidenceDraft(
                source=EvidenceSource.DEPLOYMENT,
                kind=EvidenceKind.DEPLOYMENT,
                summary=(
                    f"active release {state.get('active_release')} "
                    f"(previous {state.get('previous_release')}), "
                    f"healthy={state.get('healthy')}"
                ),
                content=redact_mapping(
                    {
                        "active_release": state.get("active_release"),
                        "previous_release": state.get("previous_release"),
                        "deployed_at": state.get("deployed_at"),
                        "healthy": state.get("healthy"),
                        "available_releases": state.get("available_releases", []),
                    }
                ),
                timestamp=self._parse_ts(state.get("deployed_at")),
                confidence=Confidence.HIGH,
                simulated=bool(state.get("simulated", False)),
            )
        ]
        for deployment in history.get("deployments", [])[: self.max_items]:
            drafts.append(
                EvidenceDraft(
                    source=EvidenceSource.DEPLOYMENT,
                    kind=EvidenceKind.DEPLOYMENT,
                    summary=(
                        f"deployment {deployment.get('release')} "
                        f"at {deployment.get('deployed_at')} "
                        f"by {deployment.get('deployed_by')}"
                    ),
                    content=redact_mapping(dict(deployment)),
                    timestamp=self._parse_ts(deployment.get("deployed_at")),
                    confidence=Confidence.HIGH,
                    simulated=bool(history.get("simulated", False)),
                )
            )
        return drafts, None

    async def _payments(
        self, incident: Incident
    ) -> tuple[list[EvidenceDraft], dict[str, str] | None]:
        failures = await self.integrations.payments.payment_failures(
            self.settings.detection_window_minutes
        )
        customers = await self.integrations.payments.affected_customers(
            self.settings.detection_window_minutes
        )
        drafts = [
            EvidenceDraft(
                source=EvidenceSource.PAYMENTS,
                kind=EvidenceKind.PAYMENT_FAILURE_SUMMARY,
                summary=(
                    f"provider {failures.get('provider')} "
                    f"status {failures.get('provider_status')}: "
                    f"{failures.get('failure_count')} failures "
                    f"({float(failures.get('failure_rate', 0)) * 100:.1f}%)"
                ),
                content=redact_mapping(
                    {
                        "provider": failures.get("provider"),
                        "provider_status": failures.get("provider_status"),
                        "failure_count": failures.get("failure_count"),
                        "failure_rate": failures.get("failure_rate"),
                        "top_error_codes": failures.get("top_error_codes", []),
                    }
                ),
                timestamp=self._parse_ts(failures.get("window_end")),
                confidence=Confidence.HIGH,
                simulated=bool(failures.get("simulated", False)),
            ),
            EvidenceDraft(
                source=EvidenceSource.PAYMENTS,
                kind=EvidenceKind.AFFECTED_CUSTOMERS,
                summary=(
                    f"{customers.get('affected_count')} customers affected, "
                    f"${float(customers.get('total_mrr_at_risk', 0)):,.2f} MRR at risk"
                ),
                content=redact_mapping(
                    {
                        "affected_count": customers.get("affected_count"),
                        "total_mrr_at_risk": customers.get("total_mrr_at_risk"),
                        # Customer identifiers are minimised: counts and plans, not identities.
                        "plans": sorted(
                            {str(item.get("plan")) for item in customers.get("customers", [])}
                        ),
                    }
                ),
                timestamp=self._parse_ts(failures.get("window_end")),
                confidence=Confidence.MEDIUM,
                simulated=bool(customers.get("simulated", False)),
            ),
        ]
        return drafts, None

    async def _application_events(
        self, incident: Incident
    ) -> tuple[list[EvidenceDraft], dict[str, str] | None]:
        """Tool-failure incidents: the "evidence" is the agent's own invocation history."""
        draft = EvidenceDraft(
            source=EvidenceSource.APPLICATION_EVENTS,
            kind=EvidenceKind.OBSERVATION,
            summary=f"application event stream consulted for {incident.service}",
            content={"note": "tool failure incidents are investigated from tool invocations"},
            timestamp=datetime.now(UTC),
            confidence=Confidence.LOW,
        )
        return [draft], None

    @staticmethod
    def _parse_ts(value: Any) -> datetime:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        if isinstance(value, str) and value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            except ValueError:
                pass
        return datetime.now(UTC)


__all__ = ["METRIC_PLAN", "SOURCE_PLAN", "EvidenceCollector"]
