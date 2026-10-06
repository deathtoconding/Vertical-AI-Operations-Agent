"""Where a migration points, when nobody says (ADR-0005, ADR-0010).

Story: DEV-001. ``scripts/entrypoint.sh`` runs ``alembic upgrade head`` before it starts the
server, so the URL resolution below is the difference between a container that migrates the
database it was configured with and one that migrates *localhost* — retries thirty times, gives
up, and never serves a request. That is how the first v1.0.0 image failed its smoke test.
"""

from __future__ import annotations

import pytest

from app.persistence.pg import database_url_from_socket, resolve_database_url

pytestmark = [pytest.mark.story("DEV-001"), pytest.mark.unit]

CONFIGURED = "postgresql+psycopg://aiops:secret@database.internal:5432/aiops"


@pytest.fixture
def socket_uri(tmp_path, monkeypatch):
    """A started local cluster, as ``make pg`` leaves it: ``.pgdata/.uri`` plus the module path."""
    monkeypatch.setattr("app.persistence.pg.DEFAULT_PGDATA", tmp_path)
    (tmp_path / ".uri").write_text(
        "postgresql://aiops@/postgres?host=/tmp/aiops-pgdata", encoding="utf-8"
    )
    return "postgresql+psycopg://aiops@/aiops?host=/tmp/aiops-pgdata"


def test_the_configured_database_wins_over_a_local_socket(socket_uri, monkeypatch) -> None:
    """The container case: no `.pgdata`, and the database is whatever the deployment configured."""
    monkeypatch.setenv("AIOPS_DATABASE_URL", CONFIGURED)
    assert resolve_database_url() == CONFIGURED


def test_an_empty_configuration_falls_through_to_the_socket(socket_uri, monkeypatch) -> None:
    monkeypatch.delenv("AIOPS_DATABASE_URL", raising=False)
    assert resolve_database_url() == socket_uri


def test_a_local_socket_is_the_last_resort_before_localhost(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AIOPS_DATABASE_URL", raising=False)
    monkeypatch.setattr("app.persistence.pg.DEFAULT_PGDATA", tmp_path)  # no cluster started
    assert resolve_database_url(database="incidents") == (
        "postgresql+psycopg://aiops:aiops@localhost:5432/incidents"
    )


def test_an_explicit_argument_beats_the_environment(monkeypatch) -> None:
    """``alembic -x url=…`` is how an operator aims a migration deliberately."""
    monkeypatch.setenv("AIOPS_DATABASE_URL", CONFIGURED)
    assert resolve_database_url("postgresql+psycopg://other@/aiops") == (
        "postgresql+psycopg://other@/aiops"
    )


def test_the_socket_uri_is_translated_into_a_driver_qualified_url() -> None:
    assert database_url_from_socket("postgresql://aiops@/postgres?host=/tmp/x", "incidents") == (
        "postgresql+psycopg://aiops@/incidents?host=/tmp/x"
    )
    with pytest.raises(ValueError, match="not a postgres URI"):
        database_url_from_socket("localhost:5432")
