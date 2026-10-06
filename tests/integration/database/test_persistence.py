"""Persistence integration tests against real PostgreSQL.

Story: OPS-011 — "core entities survive application restart" is the headline acceptance
criterion, so it is tested by actually disposing an engine, creating a new one, and reading
the rows back.

Also covered here: migration idempotency, optimistic concurrency on agent runs, the
append-only audit chain and the idempotency store.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import pathlib

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.errors import ConflictError, IdempotencyConflict
from app.domain.actions import Action as DomainAction
from app.domain.actions import ActionRequest
from app.domain.enums import (
    ActionStatus,
    AgentState,
    ApprovalDecision,
    AuditEventType,
    EvidenceKind,
    EvidenceSource,
    IncidentStatus,
    IncidentType,
    RiskLevel,
    Severity,
)
from app.domain.evidence import EvidenceDraft
from app.persistence.repositories import (
    ActionRepository,
    AgentRunRepository,
    AnomalyRepository,
    ApprovalRepository,
    AuditRepository,
    EvidenceRepository,
    IdempotencyStore,
    IncidentRepository,
    VerificationRepository,
)

pytestmark = [pytest.mark.story("OPS-011"), pytest.mark.integration]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


def incident_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "incident_type": IncidentType.API_ERROR_SPIKE.value,
        "severity": Severity.SEV2.value,
        "status": IncidentStatus.OPEN.value,
        "title": "API error rate spike on checkout-service",
        "summary": "error_rate 18.0% vs baseline 2.0%",
        "service": "checkout-service",
        "metric": "error_rate",
        "dedup_key": "checkout-service:error_rate:2026-10-06T12",
        "detected_at": dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.UTC),
    }
    values.update(overrides)
    return values


# --------------------------------------------------------------------------- #
# Round trips and restart survival
# --------------------------------------------------------------------------- #


async def test_incident_round_trip(session) -> None:
    repo = IncidentRepository(session)
    created = await repo.create(incident_values())

    assert created.id
    assert created.severity is Severity.SEV2
    assert created.status is IncidentStatus.OPEN
    assert created.is_terminal is False

    fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched.title == created.title
    assert fetched.service == "checkout-service"


async def test_core_entities_survive_a_restart(engine, database_url: str) -> None:
    """OPS-011 acceptance criterion: data outlives the process that wrote it."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        incident = await IncidentRepository(session).create(incident_values())
        evidence = await EvidenceRepository(session).persist(
            incident.id,
            [
                EvidenceDraft(
                    source=EvidenceSource.METRICS,
                    kind=EvidenceKind.METRIC_SERIES,
                    summary="error_rate rose from 2% to 18%",
                    content={"points": [2.0, 18.0]},
                ),
                EvidenceDraft(
                    source=EvidenceSource.GITHUB,
                    kind=EvidenceKind.DEPLOYMENT,
                    summary="release-42 deployed 11 minutes before onset",
                    content={"sha": "abc123", "release": "release-42"},
                ),
            ],
        )
        await AuditRepository(session).append(
            AuditEventType.INCIDENT_CREATED,
            actor="system",
            incident_id=incident.id,
            payload={"severity": incident.severity.value},
        )
        incident_id = incident.id
        evidence_ids = [item.id for item in evidence]
        await session.commit()

    # Simulate a restart: brand new engine, brand new session, nothing cached.
    async with factory() as _unused:  # keep the old one referenced for clarity
        pass
    fresh_engine = create_async_engine(database_url)
    fresh_factory = async_sessionmaker(fresh_engine, expire_on_commit=False)
    try:
        async with fresh_factory() as session:
            incident = await IncidentRepository(session).get(incident_id)
            assert incident is not None, "incidents must survive a restart"
            assert incident.title.startswith("API error rate spike")

            evidence_repo = EvidenceRepository(session)
            reloaded = await evidence_repo.get_many(evidence_ids)
            assert len(reloaded) == 2
            assert {item.source for item in reloaded} == {
                EvidenceSource.METRICS,
                EvidenceSource.GITHUB,
            }
            assert await evidence_repo.exists_for_incident(incident_id, evidence_ids) == set(
                evidence_ids
            )

            chain = await AuditRepository(session).verify_chain()
            assert chain["valid"] is True
            assert chain["entries"] == 1
    finally:
        await fresh_engine.dispose()


async def test_migrations_are_idempotent(database_url: str) -> None:
    """Running the migration twice must not fail — deploys retry migrations routinely."""
    from alembic import command
    from alembic.config import Config

    config = Config(str(REPO_ROOT / "app" / "persistence" / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "app" / "persistence" / "migrations"))
    sync_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)

    # Start from a genuinely empty database. `drop_all` is not enough: `alembic_version`
    # is not part of the SQLAlchemy metadata, so a stale head would make the upgrade a
    # no-op and the test would pass without creating anything.
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
    await engine.dispose()

    def run_upgrade() -> None:
        command.upgrade(config, "head")

    config.set_main_option("sqlalchemy.url", sync_url)
    await asyncio.get_running_loop().run_in_executor(None, run_upgrade)
    await asyncio.get_running_loop().run_in_executor(None, run_upgrade)  # idempotent

    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        result = await connection.execute(
            text("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
        )
        assert int(result.scalar_one()) >= 11
        result = await connection.execute(text("SELECT version_num FROM alembic_version"))
        assert str(result.scalar_one()) == "0001_initial_schema"
    await engine.dispose()


# --------------------------------------------------------------------------- #
# Incident specifics
# --------------------------------------------------------------------------- #


async def test_dedup_key_is_unique(session) -> None:
    repo = IncidentRepository(session)
    await repo.create(incident_values())
    duplicate = await repo.get_by_dedup_key("checkout-service:error_rate:2026-10-06T12")
    assert duplicate is not None

    # The database, not just the application, enforces the invariant. The failed INSERT
    # aborts the transaction, so roll back to a savepoint and keep the session usable.
    savepoint = await session.begin_nested()
    with pytest.raises(IntegrityError) as excinfo:
        await repo.create(incident_values(title="second incident, same dedup key"))
    assert "dedup_key" in str(excinfo.value) or "unique" in str(excinfo.value).lower()
    await savepoint.rollback()


async def test_open_incident_counts_by_severity(session) -> None:
    repo = IncidentRepository(session)
    await repo.create(incident_values(dedup_key="k1", severity=Severity.SEV1.value))
    await repo.create(
        incident_values(
            dedup_key="k2",
            severity=Severity.SEV3.value,
            incident_type=IncidentType.API_LATENCY_SPIKE.value,
        )
    )
    await repo.create(
        incident_values(
            dedup_key="k3",
            severity=Severity.SEV2.value,
            status=IncidentStatus.RESOLVED.value,
        )
    )

    counts = await repo.count_open_by_severity()
    assert counts[Severity.SEV1] == 1
    assert counts[Severity.SEV3] == 1
    assert counts[Severity.SEV2] == 0, "resolved incidents must not count as open"


async def test_stale_open_incidents_are_visible(session) -> None:
    """The 'lost critical incidents' SLI needs a way to find incidents that stalled."""
    repo = IncidentRepository(session)
    await repo.create(
        incident_values(dedup_key="stale", created_at=dt.datetime(2020, 1, 1, tzinfo=dt.UTC))
    )
    stale = await repo.list_stale_open(older_than_minutes=60)
    assert len(stale) == 1


async def test_anomaly_links_to_incident(session) -> None:
    incident = await IncidentRepository(session).create(incident_values())
    anomaly_repo = AnomalyRepository(session)
    anomaly = await anomaly_repo.create(
        {
            "incident_id": incident.id,
            "service": "checkout-service",
            "metric": "error_rate",
            "family": "error_rate",
            "baseline": 0.02,
            "observed": 0.18,
            "deviation": 0.16,
            "z_score": 9.4,
            "relative_deviation": 8.0,
            "sample_count": 30,
            "severity": Severity.SEV2.value,
            "window_start": dt.datetime(2026, 10, 6, 11, 0, tzinfo=dt.UTC),
            "window_end": dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.UTC),
            "dedup_key": "checkout-service:error_rate",
        }
    )
    linked = await anomaly_repo.list_for_incident(incident.id)
    assert [item.id for item in linked] == [anomaly.id]


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #


async def test_evidence_ids_are_human_addressable_and_unique(session) -> None:
    incident = await IncidentRepository(session).create(incident_values())
    repo = EvidenceRepository(session)
    drafts = [
        EvidenceDraft(
            source=EvidenceSource.LOGS,
            kind=EvidenceKind.LOG_CLUSTER,
            summary=f"cluster {index}",
            content={"index": index},
        )
        for index in range(5)
    ]
    items = await repo.persist(incident.id, drafts)
    ids = [item.id for item in items]
    assert len(set(ids)) == 5
    assert all(item.startswith("EV-") for item in ids)


async def test_grounding_rejects_foreign_evidence(session) -> None:
    """A diagnosis may not cite evidence belonging to a different incident."""
    repo = IncidentRepository(session)
    incident_a = await repo.create(incident_values(dedup_key="a"))
    incident_b = await repo.create(incident_values(dedup_key="b"))

    evidence_repo = EvidenceRepository(session)
    items = await evidence_repo.persist(
        incident_a.id,
        [
            EvidenceDraft(
                source=EvidenceSource.METRICS, kind=EvidenceKind.METRIC_SERIES, summary="s"
            )
        ],
    )
    resolved = await evidence_repo.exists_for_incident(incident_b.id, [items[0].id, "EV-NOPE"])
    assert resolved == set()


# --------------------------------------------------------------------------- #
# Agent runs and concurrency
# --------------------------------------------------------------------------- #


async def test_run_state_transitions_are_persisted_and_audited(session) -> None:
    incident = await IncidentRepository(session).create(incident_values())
    runs = AgentRunRepository(session)
    run = await runs.create(
        {
            "incident_id": incident.id,
            "state": AgentState.DETECTED.value,
            "actor": "system",
            "autonomy_level": "approval_required",
        }
    )
    updated, transition = await runs.transition(
        run.id, AgentState.INVESTIGATING, actor="system", reason="evidence collection"
    )
    assert updated.state is AgentState.INVESTIGATING
    assert updated.version == 2
    assert transition.from_state is AgentState.DETECTED
    assert transition.to_state is AgentState.INVESTIGATING

    history = await runs.list_transitions(run.id)
    assert len(history) == 1
    assert history[0].reason == "evidence collection"


async def test_concurrent_transitions_cannot_both_win(session) -> None:
    """Optimistic concurrency: two workers must not both advance the same run."""
    incident = await IncidentRepository(session).create(incident_values())
    runs = AgentRunRepository(session)
    run = await runs.create(
        {
            "incident_id": incident.id,
            "state": AgentState.DETECTED.value,
            "actor": "system",
            "autonomy_level": "approval_required",
        }
    )
    await runs.transition(run.id, AgentState.INVESTIGATING, actor="worker-a", expected_version=1)
    with pytest.raises(ConflictError):
        await runs.transition(run.id, AgentState.PLANNED, actor="worker-b", expected_version=1)


async def test_interrupted_runs_are_discoverable(session) -> None:
    incident = await IncidentRepository(session).create(incident_values())
    runs = AgentRunRepository(session)
    run = await runs.create(
        {
            "incident_id": incident.id,
            "state": AgentState.EXECUTING.value,
            "actor": "system",
            "autonomy_level": "approval_required",
            "updated_at": dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
        }
    )
    interrupted = await runs.list_interrupted(lease_seconds=60)
    assert [item.id for item in interrupted] == [run.id]


# --------------------------------------------------------------------------- #
# Actions, approvals, verification
# --------------------------------------------------------------------------- #


async def test_action_and_approval_binding(session) -> None:
    incident = await IncidentRepository(session).create(incident_values())
    actions = ActionRepository(session)
    request = ActionRequest(
        incident_id=incident.id,
        tool_name="deployment.rollback_simulation",
        params={"target_release": "release-41"},
    )
    action = await actions.create(
        {
            "incident_id": incident.id,
            "tool_name": request.tool_name,
            "risk": RiskLevel.HIGH.value,
            "permission": "deployment.rollback",
            "params": request.params,
            "canonical_hash": request.canonical_hash,
            "idempotency_key": request.idempotency_key,
            "requires_approval": True,
            "status": ActionStatus.AWAITING_APPROVAL.value,
        }
    )
    assert action.payload_is_intact, "stored hash must match the stored payload"
    approvals = ApprovalRepository(session)
    approval = await approvals.create(
        {
            "action_id": action.id,
            "incident_id": incident.id,
            "payload_hash": action.canonical_hash,
            "decision": ApprovalDecision.PENDING.value,
            "expires_at": dt.datetime(2099, 1, 1, tzinfo=dt.UTC),
        }
    )

    # A pending approval is not permission: binding verification requires a decision.
    assert not await approvals.verify_binding(approval.id, action=action)

    decided = await approvals.decide(
        approval.id, decision=ApprovalDecision.APPROVED, actor="oncall", role="sre"
    )
    assert decided is not None and decided.decision is ApprovalDecision.APPROVED

    assert await approvals.verify_binding(approval.id, action=action)
    assert not await approvals.verify_binding(
        approval.id,
        {"tool_name": "deployment.rollback_simulation", "params": {"target_release": "release-99"}},
    )
    # Tampering: same action id, different target release -> the approval no longer binds.
    tampered = DomainAction.model_validate(
        {**action.model_dump(), "params": {"target_release": "release-99"}}
    )
    assert not tampered.payload_is_intact, "a modified payload must not match its stored hash"
    assert not await approvals.verify_binding(approval.id, action=tampered)
    assert action.payload_is_intact, "the untouched action still verifies"

    # Approving twice is a conflict, not a silently accepted second decision.
    with pytest.raises(ConflictError):
        await approvals.decide(
            approval.id, decision=ApprovalDecision.REJECTED, actor="other", role="sre"
        )


async def test_verification_results_are_queryable(session) -> None:
    incident = await IncidentRepository(session).create(incident_values())
    repo = VerificationRepository(session)
    now = dt.datetime(2026, 10, 6, 12, 5, tzinfo=dt.UTC)
    await repo.create(
        {
            "incident_id": incident.id,
            "outcome": "SUCCESS",
            "reason": "error rate recovered",
            "checks": [{"check": "error_rate_below", "outcome": "SUCCESS"}],
            "observed": {"error_rate": 0.012},
            "started_at": now,
            "completed_at": now,
            "duration_seconds": 12.5,
        }
    )
    latest = await repo.latest_for_incident(incident.id)
    assert latest is not None and latest.outcome.value == "SUCCESS"
    counts = await repo.outcome_counts(dt.datetime(2020, 1, 1, tzinfo=dt.UTC))
    assert counts == {"SUCCESS": 1}


async def test_rollback_rate_limit_counter(session) -> None:
    """The anti-rollback-loop guard (threat T-11) needs a real count of recent rollbacks."""
    incident = await IncidentRepository(session).create(incident_values())
    actions = ActionRepository(session)
    for index in range(3):
        await actions.create(
            {
                "incident_id": incident.id,
                "tool_name": "deployment.rollback_simulation",
                "risk": RiskLevel.HIGH.value,
                "permission": "deployment.rollback",
                "params": {"target_release": f"release-{index}"},
                "canonical_hash": f"hash-{index}",
                "idempotency_key": f"key-{index}",
                "requires_approval": True,
                "status": ActionStatus.SUCCEEDED.value,
            }
        )
    count = await actions.count_recent_rollbacks(dt.datetime(2020, 1, 1, tzinfo=dt.UTC))
    assert count == 3


# --------------------------------------------------------------------------- #
# Audit chain and idempotency
# --------------------------------------------------------------------------- #


async def test_audit_chain_detects_tampering(session) -> None:
    audit = AuditRepository(session)
    await audit.append(AuditEventType.INCIDENT_CREATED, actor="system", reason="created")
    await audit.append(AuditEventType.POLICY_EVALUATED, actor="system", reason="allowed")
    await audit.append(AuditEventType.ACTION_EXECUTED, actor="system", reason="rollback")

    assert (await audit.verify_chain())["valid"] is True

    # Tamper with the middle entry, exactly as an attacker or a bad migration would.
    await session.execute(
        text(
            "UPDATE audit_events SET payload = '\"tampered\"'::jsonb "
            "WHERE seq = (SELECT min(seq) + 1 FROM audit_events)"
        )
    )
    await session.flush()
    result = await audit.verify_chain()
    assert result["valid"] is False
    assert result["broken_at_seq"] is not None


async def test_audit_payloads_are_redacted(session) -> None:
    audit = AuditRepository(session)
    row = await audit.append(
        AuditEventType.AUTHENTICATION_FAILED,
        actor="anonymous",
        payload={"authorization": "Bearer super-secret-token", "note": "ok"},
    )
    assert row.payload["authorization"] == "***"
    assert row.payload["note"] == "ok"


async def test_idempotency_replays_and_conflicts(session) -> None:
    store = IdempotencyStore(session)
    row, created = await store.reserve("test.scope", "key-1", "hash-1")
    assert created is True
    await store.complete("test.scope", "key-1", external_id="EXT-1", response={"ok": True})

    replay, created_again = await store.reserve("test.scope", "key-1", "hash-1")
    assert created_again is False
    assert replay.external_id == "EXT-1"
    assert replay.response == {"ok": True}
    assert replay.id == row.id

    with pytest.raises(IdempotencyConflict):
        await store.reserve("test.scope", "key-1", "different-hash")


async def test_tool_invocations_records_denials(session) -> None:
    """Denied attempts are recorded, because 'what the agent tried' is auditable too."""
    from app.domain.tools import ToolInvocation
    from app.persistence.repositories import ToolInvocationRepository

    repo = ToolInvocationRepository(session)
    recorded = await repo.record(
        {
            "tool_name": "deployment.rollback_simulation",
            "risk": RiskLevel.HIGH.value,
            "outcome": "denied",
            "request": {"target_release": "release-41"},
            "actor": "agent",
        }
    )
    assert recorded.outcome.value == "denied"
    assert isinstance(recorded, ToolInvocation)
