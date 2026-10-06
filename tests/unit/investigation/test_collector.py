"""Evidence collection (OPS-040).

The property under test is not "does it fetch data" but "does it stay honest when it cannot".
A collector that fails hard loses the investigation; one that silently returns fewer sources
produces a confident diagnosis built on missing data. The required behaviour is a third path:
record the degradation, keep the rest, and label the untrusted content.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest

from app.core.config import Settings
from app.core.errors import IntegrationUnavailable
from app.domain.enums import (
    EvidenceKind,
    EvidenceSource,
    IncidentStatus,
    IncidentType,
    Severity,
)
from app.domain.incidents import Incident
from app.integrations.facade import IntegrationFacade, build_integrations
from app.investigation.collector import METRIC_PLAN, SOURCE_PLAN, EvidenceCollector
from app.sandbox.simulator import get_sandbox

pytestmark = [pytest.mark.story("OPS-040"), pytest.mark.unit]

NOW = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.UTC)


@pytest.fixture(autouse=True)
def _sandbox_scenario_a() -> Iterator[None]:
    """Every test starts from the same simulated fault and leaves the sandbox clean."""
    sandbox = get_sandbox()
    sandbox.reset(scenario="A")
    yield
    sandbox.reset(scenario="normal")


@pytest.fixture
def integrations(settings: Settings) -> IntegrationFacade:
    return build_integrations(settings)


@pytest.fixture
def collector(settings: Settings, integrations: IntegrationFacade) -> EvidenceCollector:
    return EvidenceCollector(settings, integrations)


def incident(
    incident_type: IncidentType = IncidentType.API_ERROR_SPIKE, **overrides: object
) -> Incident:
    values: dict[str, object] = {
        "id": "INC-COLLECT-1",
        "incident_type": incident_type,
        "severity": Severity.SEV2,
        "status": IncidentStatus.OPEN,
        "title": "API error rate spike on checkout-service",
        "service": "checkout-service",
        "metric": "error_rate",
        "dedup_key": "dedup-collector",
        "detected_at": NOW,
    }
    values.update(overrides)
    return Incident.model_validate(values)


# --------------------------------------------------------------------------- #
# Source selection
# --------------------------------------------------------------------------- #


def test_every_incident_type_has_a_source_plan() -> None:
    for incident_type in IncidentType:
        sources = SOURCE_PLAN.get(incident_type)
        assert sources, f"{incident_type} would fall back to a guess"
        assert len(set(sources)) == len(sources), "duplicate sources waste quota"


def test_payment_incidents_look_at_the_payment_provider_first() -> None:
    assert EvidenceSource.PAYMENTS in SOURCE_PLAN[IncidentType.SUBSCRIPTION_PAYMENT_ANOMALY]
    assert "payment_failure_rate" in METRIC_PLAN[IncidentType.SUBSCRIPTION_PAYMENT_ANOMALY]


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


async def test_collects_and_normalises_evidence_for_an_error_spike(
    collector: EvidenceCollector,
) -> None:
    report = await collector.collect(incident())

    assert report.incident_id == "INC-COLLECT-1"
    assert report.degradations == []
    assert set(report.sources_queried) == {
        source.value for source in SOURCE_PLAN[IncidentType.API_ERROR_SPIKE]
    }

    sources = {draft.source for draft in report.drafts}
    assert EvidenceSource.METRICS in sources
    assert EvidenceSource.LOGS in sources
    assert EvidenceSource.GITHUB in sources
    assert EvidenceSource.DEPLOYMENT in sources

    # Evidence is ordered, so a human reading the timeline sees cause before effect.
    timestamps = [draft.timestamp for draft in report.drafts]
    assert timestamps == sorted(timestamps)

    metric_draft = next(
        draft for draft in report.drafts if draft.kind is EvidenceKind.METRIC_SERIES
    )
    assert "error_rate" in metric_draft.summary
    assert metric_draft.content["samples"] > 0
    assert metric_draft.content["peak"] >= metric_draft.content["latest"] >= 0.0
    assert metric_draft.timestamp.tzinfo is not None, "naive timestamps break ordering"


async def test_drafts_are_flagged_as_simulated_in_sandbox_mode(
    collector: EvidenceCollector,
) -> None:
    report = await collector.collect(incident())
    assert report.drafts, "sandbox scenario A must produce evidence"
    assert all(draft.simulated for draft in report.drafts), (
        "evidence from the simulator must never look real"
    )


# --------------------------------------------------------------------------- #
# Degradation
# --------------------------------------------------------------------------- #


async def test_a_failing_source_is_recorded_and_does_not_abort_the_collection(
    collector: EvidenceCollector, integrations: IntegrationFacade
) -> None:
    async def exploding_state() -> dict[str, object]:
        raise IntegrationUnavailable("deployment", "deployment provider is unreachable")

    integrations.deployment_state = exploding_state  # type: ignore[method-assign]

    report = await collector.collect(incident())

    degraded = {item["source"]: item for item in report.degradations}
    assert degraded["deployment"]["reason"] == "IntegrationUnavailable"
    assert "unreachable" in degraded["deployment"]["detail"]
    # The other sources still answered.
    assert {draft.source for draft in report.drafts} >= {
        EvidenceSource.METRICS,
        EvidenceSource.LOGS,
        EvidenceSource.GITHUB,
    }


async def test_partial_metric_failure_keeps_the_series_that_did_arrive(
    collector: EvidenceCollector, integrations: IntegrationFacade, settings: Settings
) -> None:
    original = integrations.metrics_window

    async def one_metric_missing(service: str, metric: str, window_minutes: int) -> dict[str, Any]:
        if metric == "latency_p95":
            raise IntegrationUnavailable("metrics", "latency_p95 is not exported")
        return await original(service, metric, window_minutes)

    integrations.metrics_window = one_metric_missing  # type: ignore[method-assign]

    report = await collector.collect(incident())

    partial = [item for item in report.degradations if item["reason"] == "partial"]
    assert partial, "a metric that could not be fetched must be reported, not hidden"
    assert "latency_p95" in partial[0]["detail"]
    metric_drafts = [draft for draft in report.drafts if draft.source is EvidenceSource.METRICS]
    assert metric_drafts, "the metrics that did arrive are still evidence"


async def test_a_metric_provider_that_fails_entirely_degrades_metrics_only(
    collector: EvidenceCollector, integrations: IntegrationFacade
) -> None:
    async def all_metrics_fail(service: str, metric: str, window_minutes: int) -> dict[str, Any]:
        raise IntegrationUnavailable("metrics", "metrics provider down")

    integrations.metrics_window = all_metrics_fail  # type: ignore[method-assign]

    report = await collector.collect(incident())

    degraded = {item["source"] for item in report.degradations}
    assert "metrics" in degraded
    assert EvidenceSource.LOGS in {draft.source for draft in report.drafts}


# --------------------------------------------------------------------------- #
# Untrusted content
# --------------------------------------------------------------------------- #


async def test_log_content_is_sanitised_and_injections_are_flagged(
    collector: EvidenceCollector,
) -> None:
    report = await collector.collect(incident())
    log_draft = next(draft for draft in report.drafts if draft.source is EvidenceSource.LOGS)

    assert log_draft.content["total_lines"] > 0
    assert log_draft.content["injections"], (
        "scenario A ships an injected instruction; the collector must flag it"
    )
    assert all(flag.isupper() or flag.islower() for flag in log_draft.content["injections"])
    # Flagged content stays inside the evidence (bounded) — it is data to report, not to follow.
    assert isinstance(log_draft.content["lines"], list)


async def test_customer_identities_are_minimised_in_payment_evidence(
    collector: EvidenceCollector,
) -> None:
    report = await collector.collect(
        incident(IncidentType.SUBSCRIPTION_PAYMENT_ANOMALY, metric="payment_failure_rate")
    )
    payment_drafts = [draft for draft in report.drafts if draft.source is EvidenceSource.PAYMENTS]
    assert payment_drafts, "a payment incident must collect payment evidence"
    for draft in payment_drafts:
        content = draft.content
        assert "customers" not in content, "individual customer records must not be stored"
        assert "affected_count" in content or "failure_count" in content


async def test_tool_failure_incidents_use_application_events(
    collector: EvidenceCollector,
) -> None:
    report = await collector.collect(incident(IncidentType.TOOL_FAILURE))
    assert report.sources_queried == [EvidenceSource.APPLICATION_EVENTS.value]
    assert report.drafts[0].source is EvidenceSource.APPLICATION_EVENTS
    assert report.degradations == []


async def test_collection_is_timestamped_and_bounded(
    collector: EvidenceCollector, settings: Settings
) -> None:
    report = await collector.collect(incident())
    assert report.duration_seconds >= 0.0
    for draft in report.drafts:
        if draft.kind is EvidenceKind.LOG_CLUSTER:
            assert len(draft.content["lines"]) <= settings.evidence_max_items_per_source
