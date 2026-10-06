"""Database engine, session management and health (OPS-011).

PostgreSQL is the system of record (ADR-0005). The app uses SQLAlchemy 2.0 in async mode
over psycopg 3; Alembic uses the *same URL* in sync mode, so migrations and runtime can
never disagree about the dialect.

Transaction policy (this is where "failed dependencies must not silently lose incidents"
becomes mechanical): a use case commits **before** it performs any external call. The
``session_scope`` context manager commits on success and rolls back on any exception, so a
half-written incident can never be observed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings
from app.core.errors import DatabaseUnavailable
from app.core.logging import get_logger
from app.core.telemetry import DB_POOL_IN_USE, DB_POOL_SIZE, DB_QUERY_FAILURES
from app.persistence.models.base import Base

logger = get_logger(__name__)

HEALTH_QUERY: Final[str] = "SELECT 1"


def build_engine(settings: Settings | None = None) -> AsyncEngine:
    """Create the async engine with a bounded, observable pool."""
    settings = settings or get_settings()
    url = settings.database_url
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)

    connect_args: dict[str, Any] = {"connect_timeout": int(settings.db_pool_timeout)}
    return create_async_engine(
        url,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args=connect_args,
    )


class Database:
    """Owns the engine and session factory for the application's lifetime."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.engine: AsyncEngine = build_engine(self.settings)
        self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False, autoflush=False
        )
        DB_POOL_SIZE.set(self.settings.db_pool_size)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A session that commits on success and rolls back on failure."""
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    @asynccontextmanager
    async def read_session(self) -> AsyncIterator[AsyncSession]:
        """A read-only session — never commits, so a query cannot mutate state."""
        async with self.session_factory() as session:
            yield session
            await session.rollback()

    async def health(self) -> tuple[bool, str | None]:
        """Return ``(healthy, error)``; never raises, so ``/ready`` can always answer."""
        try:
            async with self.engine.connect() as connection:
                await connection.execute(text(HEALTH_QUERY))
            self.observe_pool()
            return True, None
        except Exception as exc:
            DB_QUERY_FAILURES.inc()
            logger.warning("database_health_failed", error=str(exc))
            return False, str(exc)

    def observe_pool(self) -> None:
        """Publish pool saturation (golden signal: saturation)."""
        pool = self.engine.pool
        try:
            DB_POOL_IN_USE.set(pool.checkedout())  # type: ignore[attr-defined]
        except (AttributeError, NotImplementedError):  # pragma: no cover - NullPool etc.
            DB_POOL_IN_USE.set(0)

    async def dispose(self) -> None:
        await self.engine.dispose()

    async def require_healthy(self) -> None:
        """Raise :class:`DatabaseUnavailable` when the database is unusable."""
        healthy, error = await self.health()
        if not healthy:
            raise DatabaseUnavailable(
                "The database is unavailable.", details={"error": error or "unknown"}
            )


_database: Database | None = None


def get_database() -> Database:
    """Process-wide database handle (created lazily so tests can configure first)."""
    global _database
    if _database is None:
        _database = Database()
    return _database


def set_database(database: Database | None) -> None:
    """Override the handle — used by the application factory and by tests."""
    global _database
    _database = database


__all__ = ["Base", "Database", "build_engine", "get_database", "set_database"]
