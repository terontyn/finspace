import asyncio

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert

from app.db.models.system import SystemMetadata
from app.db.session import AsyncSessionFactory, engine


async def seed_system_metadata() -> None:
    statement = insert(SystemMetadata).values(
        key="application",
        value={"name": "Finspace", "schema_stage": "foundation"},
    )
    statement = statement.on_conflict_do_update(
        index_elements=[SystemMetadata.key],
        set_={
            "value": statement.excluded.value,
            "updated_at": func.now(),
        },
    )
    async with AsyncSessionFactory() as session:
        await session.execute(statement)
        await session.commit()


async def main() -> None:
    try:
        await seed_system_metadata()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
