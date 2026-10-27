from sqlalchemy import func, inspect, select, text

from app.db.models.system import SystemMetadata
from app.db.seed import seed_system_metadata
from app.db.session import AsyncSessionFactory, engine


async def test_postgresql_connection_and_migrated_table() -> None:
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT 1")) == 1
        table_names = await connection.run_sync(
            lambda sync_connection: inspect(sync_connection).get_table_names()
        )

    assert "system_metadata" in table_names
    assert {
        "users",
        "workspaces",
        "workspace_members",
        "accounts",
        "categories",
        "categorization_rules",
        "transactions",
        "transaction_splits",
        "audit_log",
    }.issubset(set(table_names))


async def test_seed_is_idempotent() -> None:
    await seed_system_metadata()
    await seed_system_metadata()

    async with AsyncSessionFactory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(SystemMetadata)
            .where(SystemMetadata.key == "application")
        )
        metadata = await session.scalar(
            select(SystemMetadata).where(SystemMetadata.key == "application")
        )

    assert count == 1
    assert metadata is not None
    assert metadata.value == {"name": "Finspace", "schema_stage": "foundation"}
