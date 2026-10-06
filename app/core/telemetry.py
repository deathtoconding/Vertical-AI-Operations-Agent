"""Metrics — the golden signals plus the agent-specific signals (SRE-001).

Design rules:

* one module owns the Prometheus registry, so tests can reset it deterministically;
* every metric name lives in :data:`METRIC_NAMES`, which is the contract the SLO
  document, the alert rules and the runbooks are checked against;
* **no high-cardinality labels**: labels are bounded vocabularies (role, tool, state,
  outcome). Incident ids and evidence ids belong in logs and traces, never in labels.

The zero-tolerance metrics (``unsafe_action_attempts_total``,
``audit_chain_broken_total``) exist so that "unauthorized high-risk actions: 0" is a
*measured* statement rather than an intention.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter
from typing import Final

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

REGISTRY: Final[CollectorRegistry] = CollectorRegistry(auto_describe=True)

# --------------------------------------------------------------------------- #
# Golden signals (HTTP)
# --------------------------------------------------------------------------- #

HTTP_REQUESTS = Counter(
    "aiops_http_requests_total",
    "HTTP requests by route template, method and status class.",
    ["route", "method", "status"],
    registry=REGISTRY,
)
HTTP_LATENCY = Histogram(
    "aiops_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["route", "method"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)
HTTP_IN_FLIGHT = Gauge(
    "aiops_http_requests_in_flight",
    "Requests currently being served (saturation signal).",
    registry=REGISTRY,
)

# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

DB_POOL_IN_USE = Gauge(
    "aiops_db_pool_in_use", "Checked-out connections.", registry=REGISTRY
)
DB_POOL_SIZE = Gauge("aiops_db_pool_size", "Configured pool size.", registry=REGISTRY)
DB_QUERY_FAILURES = Counter(
    "aiops_db_query_failures_total", "Failed database operations.", registry=REGISTRY
)

# --------------------------------------------------------------------------- #
# Detection and incidents
# --------------------------------------------------------------------------- #

ANOMALY_EVALUATIONS = Counter(
    "aiops_anomaly_evaluations_total",
    "Anomaly evaluations by outcome.",
    ["metric", "outcome"],  # outcome: normal | anomaly | insufficient_data
    registry=REGISTRY,
)
DETECTION_LATENCY = Histogram(
    "aiops_detection_latency_seconds",
    "Anomaly timestamp to incident creation (SLI: target p95 < 60s).",
    buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
    registry=REGISTRY,
)
INCIDENTS_CREATED = Counter(
    "aiops_incidents_created_total",
    "Incidents created by severity and type.",
    ["severity", "incident_type"],
    registry=REGISTRY,
)
INCIDENTS_DEDUPLICATED = Counter(
    "aiops_incidents_deduplicated_total",
    "Anomalies folded into an existing incident.",
    ["incident_type"],
    registry=REGISTRY,
)
INCIDENTS_OPEN = Gauge(
    "aiops_incidents_open_total",
    "Open incidents by severity (the 'lost critical incidents' SLI source).",
    ["severity"],
    registry=REGISTRY,
)

# --------------------------------------------------------------------------- #
# Evidence and investigation
# --------------------------------------------------------------------------- #

EVIDENCE_COLLECTED = Counter(
    "aiops_evidence_collected_total",
    "Evidence items persisted by source.",
    ["source"],
    registry=REGISTRY,
)
EVIDENCE_SOURCE_FAILURES = Counter(
    "aiops_evidence_source_failures_total",
    "Evidence source failures (degradations) by source and reason.",
    ["source", "reason"],
    registry=REGISTRY,
)
INVESTIGATION_LATENCY = Histogram(
    "aiops_investigation_duration_seconds",
    "Investigation start to diagnosis persisted (SLI: target p95 < 120s).",
    buckets=(0.5, 1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
    registry=REGISTRY,
)
DIAGNOSIS_GROUNDING_FAILURES = Counter(
    "aiops_diagnosis_grounding_failures_total",
    "Diagnoses rejected because their citations did not resolve to real evidence.",
    registry=REGISTRY,
)

# --------------------------------------------------------------------------- #
# LLM
# --------------------------------------------------------------------------- #

LLM_LATENCY = Histogram(
    "aiops_llm_latency_seconds",
    "LLM call latency by provider.",
    ["provider"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
    registry=REGISTRY,
)
LLM_ERRORS = Counter(
    "aiops_llm_error_total",
    "LLM errors by provider and reason.",
    ["provider", "reason"],
    registry=REGISTRY,
)
LLM_TOKENS = Counter(
    "aiops_llm_tokens_total",
    "Tokens consumed by provider and kind.",
    ["provider", "kind"],  # kind: prompt | completion
    registry=REGISTRY,
)
REASONER_FALLBACK = Counter(
    "aiops_reasoner_fallback_total",
    "Investigations that fell back to the deterministic reasoner.",
    ["reason"],
    registry=REGISTRY,
)

# --------------------------------------------------------------------------- #
# Agent runs, tools, actions
# --------------------------------------------------------------------------- #

AGENT_RUN_DURATION = Histogram(
    "aiops_agent_run_duration_seconds",
    "Agent run duration by terminal state.",
    ["state"],
    buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
    registry=REGISTRY,
)
AGENT_RUN_FAILURES = Counter(
    "aiops_agent_run_failure_total",
    "Agent run failures by stage.",
    ["stage"],
    registry=REGISTRY,
)
AGENT_RUN_RECOVERED = Counter(
    "aiops_agent_run_recovered_total",
    "Interrupted runs recovered on startup.",
    registry=REGISTRY,
)
AGENT_TRANSITIONS = Counter(
    "aiops_agent_transitions_total",
    "State machine transitions by result.",
    ["from_state", "to_state", "result"],  # result: ok | invalid
    registry=REGISTRY,
)
TOOL_INVOCATIONS = Counter(
    "aiops_tool_invocations_total",
    "Tool invocations by tool, risk level and outcome.",
    ["tool", "risk", "outcome"],
    registry=REGISTRY,
)
TOOL_LATENCY = Histogram(
    "aiops_tool_duration_seconds",
    "Tool execution latency by tool.",
    ["tool"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)
ACTION_SUCCESS_RATE = Gauge(
    "aiops_action_success_rate",
    "Rolling action success ratio (1.0 = all actions executed successfully).",
    registry=REGISTRY,
)
APPROVALS_REQUESTED = Counter(
    "aiops_approvals_requested_total",
    "Approvals requested by risk level.",
    ["risk"],
    registry=REGISTRY,
)
APPROVALS_DECIDED = Counter(
    "aiops_approvals_decided_total",
    "Approval decisions by decision.",
    ["decision"],
    registry=REGISTRY,
)
APPROVAL_LATENCY = Histogram(
    "aiops_approval_latency_seconds",
    "Human decision latency for approvals.",
    buckets=(1.0, 5.0, 15.0, 30.0, 60.0, 300.0, 900.0, 3600.0),
    registry=REGISTRY,
)
ACTION_PROPOSALS_REJECTED = Counter(
    "aiops_action_proposals_rejected_total",
    "LLM proposals dropped because the tool or parameters were invalid.",
    ["reason"],  # reason: unknown_tool | invalid_params | duplicate
    registry=REGISTRY,
)
ACTION_FAILURES = Counter(
    "aiops_action_failures_total",
    "Action failures by tool and reason.",
    ["tool", "reason"],
    registry=REGISTRY,
)
IDEMPOTENT_REPLAYS = Counter(
    "aiops_idempotent_replays_total",
    "Action/idempotency replays served from the store instead of re-executing.",
    ["scope"],
    registry=REGISTRY,
)

# --------------------------------------------------------------------------- #
# Verification and escalation
# --------------------------------------------------------------------------- #

VERIFICATION_RESULTS = Counter(
    "aiops_verification_total",
    "Verification outcomes (SLI source for verification quality).",
    ["outcome", "check"],
    registry=REGISTRY,
)
VERIFICATION_LATENCY = Histogram(
    "aiops_verification_latency_seconds",
    "Action completion to verification outcome (SLI: target p95 < 30s).",
    buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
    registry=REGISTRY,
)
ESCALATIONS = Counter(
    "aiops_escalation_total",
    "Escalations by reason.",
    ["reason"],
    registry=REGISTRY,
)

# --------------------------------------------------------------------------- #
# Security (zero-tolerance) and audit
# --------------------------------------------------------------------------- #

UNSAFE_ACTION_ATTEMPTS = Counter(
    "aiops_unsafe_action_attempts_total",
    "High/critical actions attempted by tool and outcome. "
    "outcome='executed' without approval must never increase.",
    ["tool", "outcome"],  # outcome: blocked | denied | executed
    registry=REGISTRY,
)
AUTHORIZATION_DENIALS = Counter(
    "aiops_authorization_denials_total",
    "Authorization denials by permission and role.",
    ["permission", "role"],
    registry=REGISTRY,
)
AUTH_FAILURES = Counter(
    "aiops_auth_failures_total",
    "Authentication failures by reason.",
    ["reason"],  # reason: missing_token | invalid_token | unknown_role
    registry=REGISTRY,
)
RATE_LIMIT_REJECTIONS = Counter(
    "aiops_rate_limit_rejections_total",
    "Requests rejected by the rate limiter.",
    ["scope"],
    registry=REGISTRY,
)
VALIDATION_REJECTIONS = Counter(
    "aiops_validation_rejections_total",
    "Requests rejected by input validation.",
    ["route"],
    registry=REGISTRY,
)
PROMPT_INJECTION_DETECTED = Counter(
    "aiops_prompt_injection_detected_total",
    "Untrusted content that matched an injection heuristic, by source.",
    ["source", "pattern"],
    registry=REGISTRY,
)
UNTRUSTED_TRUNCATIONS = Counter(
    "aiops_untrusted_content_truncated_total",
    "Untrusted payloads truncated before reaching the LLM or a tool.",
    ["source"],
    registry=REGISTRY,
)
AUDIT_EVENTS = Counter(
    "aiops_audit_events_total",
    "Audit events appended by event type.",
    ["event_type"],
    registry=REGISTRY,
)
AUDIT_CHAIN_BROKEN = Gauge(
    "aiops_audit_chain_broken",
    "1 when the audit hash chain fails verification, else 0 (zero-tolerance alert).",
    registry=REGISTRY,
)

# --------------------------------------------------------------------------- #
# Integrations
# --------------------------------------------------------------------------- #

INTEGRATION_REQUESTS = Counter(
    "aiops_integration_request_total",
    "Outbound integration requests by system and outcome.",
    ["system", "outcome"],
    registry=REGISTRY,
)
INTEGRATION_LATENCY = Histogram(
    "aiops_integration_request_duration_seconds",
    "Outbound integration latency by system.",
    ["system"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
    registry=REGISTRY,
)
INTEGRATION_RETRIES = Counter(
    "aiops_integration_retries_total",
    "Retries attempted by system and reason.",
    ["system", "reason"],
    registry=REGISTRY,
)
GITHUB_RATE_LIMIT_REMAINING = Gauge(
    "aiops_github_rate_limit_remaining",
    "GitHub API rate limit remaining as reported by the last response.",
    registry=REGISTRY,
)

# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #

METRIC_NAMES: Final[frozenset[str]] = frozenset(
    {
        # golden signals
        "aiops_http_requests_total",
        "aiops_http_request_duration_seconds",
        "aiops_http_requests_in_flight",
        # database
        "aiops_db_pool_in_use",
        "aiops_db_pool_size",
        "aiops_db_query_failures_total",
        # detection and incidents
        "aiops_anomaly_evaluations_total",
        "aiops_detection_latency_seconds",
        "aiops_incidents_created_total",
        "aiops_incidents_deduplicated_total",
        "aiops_incidents_open_total",
        # evidence and investigation
        "aiops_evidence_collected_total",
        "aiops_evidence_source_failures_total",
        "aiops_investigation_duration_seconds",
        "aiops_diagnosis_grounding_failures_total",
        # llm
        "aiops_llm_latency_seconds",
        "aiops_llm_error_total",
        "aiops_llm_tokens_total",
        "aiops_reasoner_fallback_total",
        # agent runtime
        "aiops_agent_run_duration_seconds",
        "aiops_agent_run_failure_total",
        "aiops_agent_run_recovered_total",
        "aiops_agent_transitions_total",
        "aiops_tool_invocations_total",
        "aiops_tool_duration_seconds",
        "aiops_action_success_rate",
        "aiops_approvals_requested_total",
        "aiops_approvals_decided_total",
        "aiops_approval_latency_seconds",
        "aiops_action_proposals_rejected_total",
        "aiops_action_failures_total",
        "aiops_idempotent_replays_total",
        # verification
        "aiops_verification_total",
        "aiops_verification_latency_seconds",
        "aiops_escalation_total",
        # security (zero tolerance) and audit
        "aiops_unsafe_action_attempts_total",
        "aiops_authorization_denials_total",
        "aiops_auth_failures_total",
        "aiops_rate_limit_rejections_total",
        "aiops_validation_rejections_total",
        "aiops_prompt_injection_detected_total",
        "aiops_untrusted_content_truncated_total",
        "aiops_audit_events_total",
        "aiops_audit_chain_broken",
        # integrations
        "aiops_integration_request_total",
        "aiops_integration_request_duration_seconds",
        "aiops_integration_retries_total",
        "aiops_github_rate_limit_remaining",
    }
)
"""Every emitted metric name. Documented SLOs, alert rules and runbooks are validated
against this set, so a runbook cannot reference a metric that does not exist."""

SLO_METRIC_MAP: Final[dict[str, str]] = {
    "api_availability": HTTP_REQUESTS._name,
    "detection_latency": DETECTION_LATENCY._name,
    "investigation_latency": INVESTIGATION_LATENCY._name,
    "verification_latency": VERIFICATION_LATENCY._name,
    "tool_execution_success": TOOL_INVOCATIONS._name,
    "lost_critical_incidents": INCIDENTS_OPEN._name,
    "unauthorized_high_risk_actions": UNSAFE_ACTION_ATTEMPTS._name,
    "agent_run_success": AGENT_RUN_FAILURES._name,
    "audit_integrity": AUDIT_CHAIN_BROKEN._name,
}


def _normalise(name: str) -> str:
    """Counters expose ``_total`` in the exposition but not in ``Metric._name``."""
    return name[: -len("_total")] if name.endswith("_total") else name


def registry_metric_names() -> frozenset[str]:
    """Family names actually registered, normalised for comparison with METRIC_NAMES."""
    return frozenset(_normalise(metric.name) for metric in REGISTRY.collect())


def assert_metric_contract() -> None:
    """Raise if the documented metric set and the registered set have drifted apart.

    Called from tests; a runbook or SLO referencing a metric that does not exist is a
    documentation defect, and a metric that exists but is undocumented is a blind spot.
    """
    declared = {_normalise(name) for name in METRIC_NAMES}
    registered = registry_metric_names()
    undocumented = sorted(registered - declared)
    missing = sorted(declared - registered)
    if undocumented or missing:
        raise AssertionError(
            f"metric contract drift: undocumented={undocumented} missing={missing}"
        )


def render_metrics() -> bytes:
    """Prometheus text exposition for ``GET /metrics``."""
    return generate_latest(REGISTRY)


@contextmanager
def observe(histogram: Histogram, **labels: str) -> Iterator[None]:
    """Time a block into a histogram, recording the duration even when it raises."""
    start = perf_counter()
    labelled = histogram.labels(**labels) if labels else histogram
    try:
        yield
    finally:
        labelled.observe(perf_counter() - start)


def reset_for_tests() -> None:
    """Clear all metric state. Used by fixtures so assertions are order-independent."""
    for collector in list(REGISTRY._collector_to_names):
        if collector not in (DB_POOL_SIZE, AUDIT_CHAIN_BROKEN, ACTION_SUCCESS_RATE,
                             INCIDENTS_OPEN, HTTP_IN_FLIGHT, DB_POOL_IN_USE,
                             GITHUB_RATE_LIMIT_REMAINING):
            REGISTRY.unregister(collector)
