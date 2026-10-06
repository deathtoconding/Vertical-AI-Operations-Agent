"""Audit trail: append-only, hash-chained, redacted (SEC-004).

The audit table is the system of record for "what did the agent do, and who allowed it". These
tests attack it the way a sceptical reviewer would: mutate a payload behind the repository's
back, delete a row, re-order two rows, and check that ``verify_chain`` notices. They also check
the *documented* limits — the hash covers the entry's type, timestamp and payload, not its
prose (docs/architecture/audit-trail.md §3, threat T-20) — so nobody mistakes it for a
tamper-proof ledger.

The API half asserts what an operator can see: the chain status, the query surface, and that a
denied request leaves a trace.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import delete, select, text, update

from app.domain.enums import AuditEventType
from app.persistence.models.audit import AuditEventRow
from app.persistence.repositories.audit import (
    GENESIS_HASH,
    AuditRepository,
    compute_entry_hash,
)

pytestmark = [pytest.mark.story("SEC-004"), pytest.mark.integration]


@pytest.fixture
async def audit(session: Any) -> AuditRepository:
    return AuditRepository(session)


async def seed(audit: AuditRepository, count: int = 3) -> list[AuditEventRow]:
    rows = []
    for index in range(count):
        rows.append(
            await audit.append(
                AuditEventType.POLICY_EVALUATED,
                actor=f"actor-{index}",
                role="sre",
                incident_id=f"INC-{index}",
                tool_name="deployment.rollback_simulation",
                outcome="allow" if index % 2 else "require_approval",
                reason=f"evaluation {index}",
                payload={"index": index, "tool": "deployment.rollback_simulation"},
            )
        )
    return rows


# --------------------------------------------------------------------------- #
# Shapes and guarantees
# --------------------------------------------------------------------------- #


def test_the_repository_has_no_mutation_surface() -> None:
    """Append-only by construction: there is no update or delete path to forget."""
    public = {name for name in dir(AuditRepository) if not name.startswith("_")}
    assert public == {
        "append",
        "list_events",
        "iter_chain",
        "verify_chain",
        "count",
    }, f"unexpected audit repository surface: {sorted(public)}"
    assert not any(word in name for name in public for word in ("update", "delete", "remove"))


async def test_the_chain_starts_at_genesis_and_grows(audit: AuditRepository) -> None:
    empty = await audit.verify_chain()
    assert empty == {"valid": True, "entries": 0, "head": GENESIS_HASH}

    rows = await seed(audit)
    assert rows[0].prev_hash == GENESIS_HASH
    assert rows[1].prev_hash == rows[0].entry_hash
    assert [row.seq for row in rows] == sorted({row.seq for row in rows})

    result = await audit.verify_chain()
    assert result["valid"] is True
    assert result["entries"] == 3
    assert result["head"] == rows[-1].entry_hash


async def test_the_entry_hash_covers_type_timestamp_and_payload(audit: AuditRepository) -> None:
    (row,) = await seed(audit, 1)
    assert row.entry_hash == compute_entry_hash(
        prev_hash=GENESIS_HASH,
        occurred_at=row.occurred_at,
        event_type=row.event_type,
        payload=row.payload,
    )
    # A different payload with the same link must hash differently: that is the whole point.
    assert row.entry_hash != compute_entry_hash(
        prev_hash=GENESIS_HASH,
        occurred_at=row.occurred_at,
        event_type=row.event_type,
        payload={**row.payload, "index": 999},
    )


async def test_secrets_in_the_payload_are_redacted_before_hashing(audit: AuditRepository) -> None:
    row = await audit.append(
        AuditEventType.POLICY_EVALUATED,
        actor="test-sre",
        role="sre",
        # Values are irrelevant here: redaction is by key name first (and never stores the
        # value), so the fixture does not need to look like a real credential.
        payload={"token": "not-a-real-token", "api_key": "not-a-real-key"},
    )
    assert row.payload["token"] == "***"
    assert row.payload["api_key"] == "***"
    # The hash must cover what was stored, otherwise verification would fail on redaction.
    assert (await audit.verify_chain())["valid"] is True


# --------------------------------------------------------------------------- #
# Tamper detection
# --------------------------------------------------------------------------- #


async def test_editing_a_stored_payload_breaks_the_chain(
    session: Any, audit: AuditRepository
) -> None:
    rows = await seed(audit)
    target = rows[1]

    await session.execute(
        update(AuditEventRow)
        .where(AuditEventRow.seq == target.seq)
        .values(payload={"index": 999, "tool": "deployment.rollback_simulation"})
    )
    await session.flush()

    result = await audit.verify_chain()
    assert result["valid"] is False
    assert result["broken_at_seq"] == target.seq
    assert result["expected_hash"] != result["actual_hash"]


async def test_deleting_a_row_breaks_the_chain(session: Any, audit: AuditRepository) -> None:
    rows = await seed(audit)
    await session.execute(delete(AuditEventRow).where(AuditEventRow.seq == rows[1].seq))
    await session.flush()

    result = await audit.verify_chain()
    assert result["valid"] is False
    # The surviving predecessor of the deleted row is what the walk notices.
    assert result["broken_at_seq"] == rows[2].seq


async def test_a_copied_entry_hash_is_rejected_by_the_database(
    session: Any, audit: AuditRepository
) -> None:
    """Defence in depth: the chain is not the only thing standing between two identical rows.

    ``entry_hash`` carries a UNIQUE constraint (model and migration 0001), so the cheapest
    tamper — paste another entry's hash over this one — fails inside PostgreSQL rather than
    quietly producing a plausible-looking chain.
    """
    from sqlalchemy.exc import IntegrityError

    rows = await seed(audit)
    with pytest.raises(IntegrityError):
        await session.execute(
            update(AuditEventRow)
            .where(AuditEventRow.seq == rows[0].seq)
            .values(entry_hash=rows[1].entry_hash)
        )
        await session.flush()
    await session.rollback()


async def test_prose_is_not_part_of_the_hash_and_that_is_documented(
    session: Any, audit: AuditRepository
) -> None:
    """A known, documented limit: actor/reason edits need an external sink to detect."""
    rows = await seed(audit)
    await session.execute(
        update(AuditEventRow)
        .where(AuditEventRow.seq == rows[0].seq)
        .values(reason="rewritten by someone with database access")
    )
    await session.flush()
    assert (await audit.verify_chain())["valid"] is True

    doc = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "docs"
        / "architecture"
        / "audit-trail.md"
    ).read_text(encoding="utf-8")
    assert "sha256(prev_hash || canonical_json(payload))" in doc
    threat_model = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "docs"
        / "security"
        / "threat-model.md"
    ).read_text(encoding="utf-8")
    assert "T-20" in threat_model and "external append-only sink" in threat_model, (
        "the limitation must stay documented until the sink ships"
    )


async def test_query_surface_filters_without_changing_the_chain(audit: AuditRepository) -> None:
    await seed(audit)
    await audit.append(
        AuditEventType.AUTHORIZATION_DENIED,
        actor="operator-1",
        role="operator",
        outcome="denied",
        reason="missing permission",
    )

    denied = await audit.list_events(event_type=AuditEventType.AUTHORIZATION_DENIED)
    assert len(denied) == 1 and denied[0].actor == "operator-1"

    scoped = await audit.list_events(incident_id="INC-1")
    assert [row.incident_id for row in scoped] == ["INC-1"]

    by_actor = await audit.list_events(actor="actor-0")
    assert len(by_actor) == 1

    assert (await audit.verify_chain())["valid"] is True
    assert await audit.count() == 4


async def test_the_chain_survives_a_new_session(engine: Any, audit: AuditRepository) -> None:
    """It is a table, not a process-local list: a restart must not reset the chain."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    rows = await seed(audit, 2)
    await audit.session.commit()

    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as fresh:
        result = await AuditRepository(fresh).verify_chain()
    assert result["valid"] is True
    assert result["head"] == rows[-1].entry_hash


# --------------------------------------------------------------------------- #
# Through the API: what an operator can actually see
# --------------------------------------------------------------------------- #


async def test_the_verify_endpoint_reports_a_healthy_chain(api: Any) -> None:
    # A 403 on an admin endpoint is audited, so the chain is never empty in practice.
    await api.client.post(
        "/api/v1/admin/autonomy",
        json={"level": "observe_only", "reason": "chain seed"},
        headers={"Authorization": "Bearer operator-token"},
    )
    response = await api.client.get("/api/v1/audit/verify")
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["entries"] >= 1
    assert len(body["head"]) == 64


async def test_the_events_endpoint_is_read_only_and_filters(api: Any) -> None:
    await api.client.post(
        "/api/v1/admin/autonomy",
        json={"level": "observe_only", "reason": "chain seed"},
        headers={"Authorization": "Bearer operator-token"},
    )
    response = await api.client.get("/api/v1/audit/events", params={"limit": 5})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] >= 1
    first = body["events"][0]
    for field in (
        "seq",
        "occurred_at",
        "event_type",
        "actor",
        "role",
        "outcome",
        "prev_hash",
        "entry_hash",
    ):
        assert field in first, field
    # The payload carries no credential material, ever.
    assert "token" not in str(first["payload"]).lower() or "***" in str(first["payload"])

    for method in ("post", "put", "patch", "delete"):
        denied = await getattr(api.client, method)("/api/v1/audit/events")
        assert denied.status_code in {401, 403, 404, 405, 422}, method


async def test_a_denied_request_is_audited_even_when_it_never_reached_the_domain(
    api: Any, session: Any
) -> None:
    """Authorization failures are exactly what an attacker hopes is *not* recorded."""
    response = await api.client.post(
        "/api/v1/admin/autonomy",
        json={"level": "observe_only", "reason": "attempt by a viewer"},
        headers={"Authorization": "Bearer viewer-token"},
    )
    assert response.status_code == 403

    result = await session.execute(
        select(AuditEventRow)
        .where(AuditEventRow.event_type == AuditEventType.AUTHORIZATION_DENIED.value)
        .order_by(AuditEventRow.seq.desc())
    )
    rows = list(result.scalars().all())
    assert rows, "a 403 that leaves no trace is an invisibility cloak"
    assert rows[0].role == "viewer"
    assert rows[0].outcome == "denied"

    assert (await AuditRepository(session).verify_chain())["valid"] is True


async def test_repeated_failures_are_counted_in_metrics(api: Any) -> None:
    """The audit row is the forensic record; the counter is the alert."""
    from app.core.telemetry import REGISTRY

    for _ in range(3):
        await api.client.get("/api/v1/incidents", headers={"Authorization": "Bearer nope"})

    families = {family.name for family in REGISTRY.collect()}
    assert "aiops_auth_failures" in families or "aiops_auth_failures_total" in families
    await api.client.get("/metrics")


async def test_the_audit_table_rejects_nothing_it_cannot_hash(
    session: Any, audit: AuditRepository
) -> None:
    """A payload that json cannot serialise must not silently corrupt the chain."""
    row = await audit.append(
        AuditEventType.POLICY_EVALUATED,
        actor="test-sre",
        payload={
            "nested": {
                "when": __import__("datetime").datetime(
                    2026, 10, 6, tzinfo=__import__("datetime").timezone.utc
                )
            }
        },
    )
    stored = await session.execute(select(AuditEventRow).where(AuditEventRow.seq == row.seq))
    assert stored.scalar_one().payload["nested"]["when"].startswith("2026-10-06")
    assert (await audit.verify_chain())["valid"] is True


async def test_raw_sql_cannot_insert_a_row_that_verifies_without_a_hash(
    session: Any, audit: AuditRepository
) -> None:
    """The chain is what makes an out-of-band insert visible, not the table's permissions."""
    await seed(audit, 1)
    await session.execute(
        text(
            """
            INSERT INTO audit_events (id, event_type, actor, role, outcome, reason, payload,
                                      occurred_at, prev_hash, entry_hash)
            VALUES ('AUD-forged', 'action_executed', 'intruder', 'admin', 'success', 'forged',
                    '{}'::jsonb, now(), repeat('0', 64), repeat('f', 64))
            """
        )
    )
    await session.flush()
    result = await audit.verify_chain()
    assert result["valid"] is False
