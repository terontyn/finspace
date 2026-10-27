from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings

if settings.environment == "test":
    engine = create_async_engine(settings.database_url_value, poolclass=NullPool)
else:
    engine = create_async_engine(settings.database_url_value, pool_pre_ping=True)
AsyncSessionFactory = async_sessionmaker(engine, expire_on_commit=False)
ForecastSessionFactory = async_sessionmaker(
    engine.execution_options(isolation_level="REPEATABLE READ"),
    expire_on_commit=False,
)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionFactory() as session:
        yield session


async def get_forecast_session() -> AsyncIterator[AsyncSession]:
    """Yield one PostgreSQL repeatable-read, read-only forecast snapshot."""
    async with ForecastSessionFactory() as session, session.begin():
        # The engine applies the isolation level before this transaction begins.
        # This is deliberately the first SQL statement in the forecast session.
        await session.execute(text("SET TRANSACTION READ ONLY"))
        yield session
