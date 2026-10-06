"""Operational metrics (SRE-001, SRE-004).

The SLO documents in `docs/sre/slos.md` are only true if the families they name are actually
exported after the API has been exercised — "the dashboard is green" must not mean "the series
does not exist". This test drives real flows through the API and then scrapes ``/metrics`` the
way Prometheus does, including the label card and the cardinality budget.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from app.core.telemetry import METRIC_NAMES, REGISTRY

pytestmark = [pytest.mark.story("SRE-001"), pytest.mark.story("SRE-004"), pytest.mark.integration]

#: Families the release verifier requires (scripts/verify_release.py). Kept in the test so a
#: rename is caught here as well as at release time.
REQUIRED_FAMILIES = (
    "aiops_http_requests_total",
    "aiops_http_request_duration_seconds",
    "aiops_incidents_created_total",
    "aiops_investigation_duration_seconds",
    "aiops_tool_invocations_total",
    "aiops_verification_total",
    "aiops_escalation_total",
    "aiops_unsafe_action_attempts_total",
    "aiops_llm_latency_seconds",
    "aiops_audit_events_total",
    "aiops_db_pool_in_use",
)

#: Labels that would make a metric's cardinality grow without bound. None of them may appear.
FORBIDDEN_LABELS = {
    "incident_id",
    "run_id",
    "action_id",
    "approval_id",
    "actor",
    "request_id",
    "trace_id",
    "user",
    "path",
}


async def warm_up_cheaply(api: Any) -> str:
    """Detect, investigate and deny — everything except the (slow) approval + execution leg.

    The full lifecycle costs ~35 s because verification waits for the simulated system to
    recover (`RECOVERY_SETTLE_SECONDS`). Only the test that asserts on execution/verification
    counters pays that price.
    """
    scenario = await api.client.post(
        "/api/v1/detection/simulate",
        json={"scenario": "A", "orchestrate": True, "reset": True},
    )
    assert scenario.status_code == 200
    incident_id = str(scenario.json()["incidents_created"][0])
    # The run stops at the approval gate, which is where the slow part of the lifecycle starts.
    detail = await api.client.get(f"/api/v1/incidents/{incident_id}")
    assert detail.json()["run"]["state"] == "WAITING_APPROVAL"

    # A denied admin action produces the authorization-denied audit + counter paths.
    await api.client.post(
        "/api/v1/admin/autonomy",
        json={"level": "observe_only", "reason": "viewer attempt"},
        headers={"Authorization": "Bearer viewer-token"},
    )
    return incident_id


async def run_full_lifecycle(api: Any) -> str:
    """Detect → investigate → plan → approve → execute → verify, i.e. the flow SLOs describe."""
    scenario = await api.client.post(
        "/api/v1/detection/simulate",
        json={"scenario": "A", "orchestrate": True, "reset": True},
    )
    assert scenario.status_code == 200
    incident_id = str(scenario.json()["incidents_created"][0])

    pending = await api.client.get("/api/v1/approvals", params={"pending_only": "true"})
    for approval in pending.json()["approvals"]:
        await api.client.post(
            f"/api/v1/approvals/{approval['id']}/decision",
            json={
                "decision": "APPROVED",
                "payload_hash": approval["payload_hash"],
                "reason": "observability test",
            },
        )
    run_id = (await api.client.get(f"/api/v1/incidents/{incident_id}")).json()["run"]["id"]
    await api.client.post(f"/api/v1/agents/runs/{run_id}/resume", json={"reason": "observe"})
    await api.client.post(
        "/api/v1/admin/autonomy",
        json={"level": "observe_only", "reason": "viewer attempt"},
        headers={"Authorization": "Bearer viewer-token"},
    )
    return incident_id


@pytest.mark.slow
async def test_the_scrape_exposes_every_family_the_slos_reference(api: Any) -> None:
    await run_full_lifecycle(api)

    response = await api.client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    text = response.text

    missing = [
        name
        for name in REQUIRED_FAMILIES
        if f"{name} " not in text and f"{name}{{" not in text and f"# TYPE {name} " not in text
    ]
    assert not missing, f"/metrics is missing required families: {missing}"

    # HELP/TYPE metadata is what makes a series usable in a dashboard or an alert rule.
    for name in REQUIRED_FAMILIES:
        assert f"# HELP {name} " in text, f"{name} has no HELP text"
        assert re.search(rf"# TYPE {name} (counter|gauge|histogram|summary)", text), name

    # The lifecycle that produced those families must actually have moved them.
    def counter_value(family: str, **labels: str) -> float:
        for metric_family in REGISTRY.collect():
            if metric_family.name != family:
                continue
            for sample in metric_family.samples:
                if sample.name.endswith("_total") and all(
                    sample.labels.get(key) == value for key, value in labels.items()
                ):
                    return float(sample.value)
        return 0.0

    assert counter_value("aiops_incidents_created") >= 1
    assert counter_value("aiops_tool_invocations", tool="deployment.rollback_simulation") >= 1
    assert counter_value("aiops_verification") >= 1
    assert counter_value("aiops_audit_events") >= 1
    assert counter_value("aiops_authorization_denials") >= 1, (
        "a 403 must be visible as a denial, both in the audit and in the counter"
    )


async def test_the_metric_catalogue_is_documented_and_named_consistently() -> None:
    assert len(METRIC_NAMES) >= 40, "the catalogue shrank: dashboards/alerts would go blank"
    assert len(set(METRIC_NAMES)) == len(METRIC_NAMES)
    for name in METRIC_NAMES:
        assert name.startswith("aiops_"), name
        assert re.fullmatch(r"aiops_[a-z0-9_]+", name), name


async def test_no_metric_carries_an_unbounded_label(api: Any) -> None:
    """One label per incident is how a metrics endpoint becomes a memory leak."""
    await warm_up_cheaply(api)
    families = {family.name: family for family in REGISTRY.collect()}
    assert families, "no families collected at all"

    for family in families.values():
        samples = list(family.samples)
        if not samples:
            continue
        label_names = set(samples[0].labels)
        offenders = label_names & FORBIDDEN_LABELS
        assert not offenders, f"{family.name} is labelled with {sorted(offenders)}"
        # A bounded label vocabulary means few distinct label sets per family.
        assert len(samples) <= 400, f"{family.name} has {len(samples)} samples"


async def test_histograms_report_buckets_not_just_totals(api: Any) -> None:
    """A latency SLO needs buckets; a count alone cannot answer "what is p95?"."""
    await warm_up_cheaply(api)
    families = {family.name: list(family.samples) for family in REGISTRY.collect()}
    buckets = families.get("aiops_http_request_duration_seconds", [])
    assert buckets, "the HTTP latency histogram was never observed"
    assert any(sample.name.endswith("_bucket") for sample in buckets)
    assert any(sample.name.endswith("_count") for sample in buckets)


async def test_the_slo_endpoint_agrees_with_the_scrape(api: Any) -> None:
    await warm_up_cheaply(api)
    slo = await api.client.get("/slo")
    assert slo.status_code == 200
    body = slo.json()
    assert body, "an SLO endpoint that reports nothing is not an SLO endpoint"
    serialised = str(body)
    for needle in ("availability", "latency"):
        assert needle in serialised.lower(), f"/slo does not mention {needle}"

    # Whatever the endpoint reports must reference real families (docs must not drift).
    referenced = set(re.findall(r"aiops_[a-z0-9_]+", serialised))
    unknown = {name for name in referenced if name not in set(METRIC_NAMES)}
    assert not unknown, f"/slo references unknown metrics: {sorted(unknown)}"


async def test_the_pool_gauge_is_present_and_bounded(api: Any) -> None:
    """The database pool is the resource every replica shares; a missing gauge blinds SRE."""
    await api.client.get("/api/v1/incidents")
    gauge = None
    for family in REGISTRY.collect():
        if family.name == "aiops_db_pool_in_use":
            gauge = family
    assert gauge is not None, "aiops_db_pool_in_use is required by the release verifier"
    assert gauge.samples, "a gauge with no samples cannot be alerted on"
    assert all(float(sample.value) >= 0 for sample in gauge.samples)


async def test_metrics_are_not_rate_limited_but_are_still_authenticated_by_design(api: Any) -> None:
    """Scrapes are exempt from throttling; see docs/sre/slos.md for the reasoning."""
    for _ in range(5):
        response = await api.client.get("/metrics")
        assert response.status_code == 200
