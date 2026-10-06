"""Shared test fixtures.

Principles:

* integration tests run against **real PostgreSQL** (ADR-0010) and *skip with a reason* when
  no server is available, rather than passing on a lie;
* every test gets a clean schema, so test order cannot matter;
* the settings object is built explicitly per test — no hidden environment mutation.

The PostgreSQL server is discovered in this order:
1. ``AIOPS_TEST_DATABASE_URL`` (used by CI, which starts a service container);
2. a local ``pgserver`` data directory at ``.pgdata`` (``make pg``);
3. otherwise integration tests skip.
"""

from __future__ import annotations

import os
import pathlib
import sys
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import pytest_asyncio
from pydantic import SecretStr

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(REPO_ROOT))

from app.core.config import Environment, IntegrationsMode, Settings  # noqa: E402
from app.core.telemetry import reset_for_tests  # noqa: E402

DEV_TOKENS = (
    "viewer-token:viewer:test-viewer,operator-token:operator:test-operator,"
    "sre-token:sre:test-sre,admin-token:admin:test-admin"
)


@pytest.fixture(autouse=True)
def _clean_metrics() -> Iterator[None]:
    """Metrics must not leak between tests, otherwise assertions become order-dependent."""
    reset_for_tests()
    yield
    reset_for_tests()


@pytest.fixture
def _base_settings() -> Settings:
    """Settings for a sandbox-mode test. Secrets are placeholders by construction.

    Kept separate from :func:`settings` so a test module can override ``settings`` (and thereby
    the application the shared ``api`` fixture builds) while deriving from the same baseline.
    """
    return Settings(
        env=Environment.TEST,
        log_level="WARNING",
        api_tokens=DEV_TOKENS,
        integrations_mode=IntegrationsMode.SANDBOX,
        database_url=_test_database_url()
        or "postgresql+psycopg://aiops:aiops@localhost:5432/aiops",
        llm_api_key=SecretStr(""),
        tracing_enabled=False,
        rate_limit_requests_per_minute=10_000,
        detection_min_samples=6,
        detection_window_minutes=30,
    )


@pytest.fixture
def settings(_base_settings: Settings) -> Settings:
    """The settings every suite uses unless a module overrides this fixture."""
    return _base_settings


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #


def _test_database_url() -> str | None:
    explicit = os.environ.get("AIOPS_TEST_DATABASE_URL")
    if explicit:
        return explicit
    from app.persistence.pg import database_url_from_socket, pgdata_uri

    cached = pgdata_uri(REPO_ROOT / ".pgdata")
    if cached:
        return database_url_from_socket(cached, os.environ.get("AIOPS_TEST_DB", "aiops"))
    return None


def _postgres_available(url: str | None) -> bool:
    if not url:
        return False
    try:
        import psycopg

        sync_url = url.replace("postgresql+psycopg://", "postgresql://", 1)
        with psycopg.connect(sync_url, connect_timeout=5) as connection:
            connection.execute("SELECT 1")
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def database_url() -> str:
    url = _test_database_url()
    if not _postgres_available(url):
        if os.environ.get("AIOPS_TEST_DATABASE_URL"):
            # An explicitly configured database that does not answer is a broken environment, not
            # a reason to test less: skipping here is what kept a whole CI test job green while
            # every schema-dependent suite never ran, and only the coverage floor noticed.
            pytest.fail(
                "AIOPS_TEST_DATABASE_URL is configured but nothing answered there "
                "(the suites do not fake the system of record)"
            )
        pytest.skip(
            "no PostgreSQL available — run `make pg` or set AIOPS_TEST_DATABASE_URL "
            "(integration tests do not fake the system of record)"
        )
    assert url is not None
    return url


@pytest_asyncio.fixture
async def engine(database_url: str) -> AsyncIterator[Any]:
    from sqlalchemy.ext.asyncio import create_async_engine

    import app.persistence.models  # noqa: F401  (register tables)
    from app.persistence.database import Base

    test_engine = create_async_engine(database_url, poolclass=None)
    async with test_engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield test_engine
    finally:
        async with test_engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await test_engine.dispose()


@pytest_asyncio.fixture
async def session(engine: Any) -> AsyncIterator[Any]:
    """A session bound to a clean schema, committed automatically at the end of the test."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as db_session:
        yield db_session
        await db_session.commit()


@pytest_asyncio.fixture
async def database(engine: Any) -> AsyncIterator[Any]:
    """A :class:`Database` bound to the test engine (for API tests that need a DB)."""
    from app.persistence.database import Database

    handle = Database.__new__(Database)
    handle.settings = None  # type: ignore[assignment]
    handle.engine = engine
    from sqlalchemy.ext.asyncio import async_sessionmaker

    handle.session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    yield handle


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #


@pytest.fixture
def fixed_now() -> Any:
    """A stable UTC timestamp for deterministic assertions."""
    import datetime as dt

    return dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=dt.UTC)


# --------------------------------------------------------------------------- #
# The application under test
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def api(settings: Settings, engine: Any) -> AsyncIterator[Any]:
    """The real application over ASGI (middleware, auth and transactions included).

    Defined at the top level so the integration, security and end-to-end suites all run the
    same object. It depends on ``engine``, which is what creates the schema and skips the test
    when no PostgreSQL is available.
    """
    from tests.support.app import running_app

    async with running_app(settings) as handle:
        yield handle


@pytest.fixture
def live_server(settings: Settings, engine: Any) -> Iterator[Any]:
    """uvicorn on a real socket, for tests that drive HTTP *scripts* rather than the ASGI app."""
    from tests.support.app import live_server as serve

    with serve(settings) as handle:
        yield handle
