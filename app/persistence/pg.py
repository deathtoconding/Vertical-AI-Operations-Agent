"""Helpers for talking to a local PostgreSQL over a unix socket (ADR-0010).

The socket URI that ``pgserver`` returns is not directly usable as a SQLAlchemy URL
because SQLAlchemy needs the driver named in the scheme. Concentrating the translation in
one place means tests, the demo profile and the CLI cannot drift apart.
"""

from __future__ import annotations

import os
import pathlib
from typing import Final

DEFAULT_PGDATA: Final[pathlib.Path] = pathlib.Path(__file__).resolve().parents[2] / ".pgdata"


def pgdata_uri(pgdata: pathlib.Path | None = None) -> str | None:
    """Return the cached socket URI for a data directory, if one has been started."""
    directory = pgdata or DEFAULT_PGDATA
    uri_file = directory / ".uri"
    if uri_file.exists():
        value = uri_file.read_text(encoding="utf-8").strip()
        return value or None
    return None


def database_url_from_socket(socket_uri: str, database: str = "aiops") -> str:
    """Translate ``postgresql://user@/db?host=/dir`` into a SQLAlchemy psycopg URL."""
    if "://" not in socket_uri:
        raise ValueError(f"not a postgres URI: {socket_uri!r}")
    scheme, rest = socket_uri.split("://", 1)
    if scheme.startswith("postgresql+psycopg"):
        return socket_uri
    # rest looks like: postgres:@/postgres?host=/path/to/pgdata
    credentials, _, tail = rest.partition("@")
    _, _, query = tail.partition("?")
    url = f"postgresql+psycopg://{credentials}@/{database}"
    if query:
        url = f"{url}?{query}"
    return url


def resolve_database_url(explicit: str | None = None, database: str = "aiops") -> str:
    """Resolve a usable database URL: explicit, ``AIOPS_DATABASE_URL``, socket, localhost.

    The environment variable comes *before* the local socket, because the socket only exists on a
    developer machine: a container has no ``.pgdata``, so `alembic upgrade head` in the image
    resolved to localhost and migrated a database the application never connected to — the
    entrypoint retried thirty times and gave up, and the container never served a request. The
    precedence mirrors :class:`~app.core.config.Settings`, so a migration cannot be aimed at a
    different database than the app (ADR-0005, ADR-0010).
    """
    if explicit:
        return explicit
    configured = os.environ.get("AIOPS_DATABASE_URL")
    if configured:
        return configured
    cached = pgdata_uri()
    if cached:
        return database_url_from_socket(cached, database)
    return f"postgresql+psycopg://aiops:aiops@localhost:5432/{database}"


__all__ = [
    "DEFAULT_PGDATA",
    "database_url_from_socket",
    "pgdata_uri",
    "resolve_database_url",
]
