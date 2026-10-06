"""Alembic environment.

Migrations run with a *synchronous* engine over the same psycopg dialect the async
application uses (ADR-0005). Keeping the URL resolution in one helper means a migration can
never be aimed at a different database than the app.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(REPO_ROOT))

# Importing the models package registers every table on Base.metadata.
import app.persistence.models  # noqa: E402,F401
from app.persistence.database import Base  # noqa: E402
from app.persistence.pg import resolve_database_url  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Resolve the URL: explicit CLI -x, then the config file, then environment/socket.
_x_args = context.get_x_argument(as_dictionary=True)
_url = _x_args.get("url") or config.get_main_option("sqlalchemy.url") or resolve_database_url()
# Alembic drives a sync engine; psycopg serves both modes from the same URL.
_sync_url = _url.replace("postgresql+psycopg://", "postgresql+psycopg://", 1)
config.set_main_option("sqlalchemy.url", _sync_url)


def run_migrations_offline() -> None:
    context.configure(
        url=_sync_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
