import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.core.config import settings
from app.core.test_database_safety import (
    DatabaseSafetyError,
    assert_test_database_marker_sync,
    validate_test_database_target,
)
from app.db import models  # noqa: F401
from app.db.base import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url_value)

migration_test_cycle = os.environ.get("MIGRATION_TEST_CYCLE", "").casefold() == "true"
try:
    if settings.testing or migration_test_cycle:
        validate_test_database_target(
            settings.database_url_value,
            environ=os.environ,
            expected_run_id=os.environ.get("TEST_RUN_ID"),
        )
    if migration_test_cycle:
        assert_test_database_marker_sync(
            settings.database_url_value,
            expected_run_id=os.environ.get("TEST_RUN_ID"),
        )
except DatabaseSafetyError as exc:
    raise RuntimeError(f"TEST DATABASE SAFETY: {exc}") from exc

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url_value,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
