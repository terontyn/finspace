from redis.asyncio import Redis
from sqlalchemy import text

from app.core.config import settings
from app.db.session import engine


async def check_database() -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def check_redis() -> None:
    client = Redis.from_url(settings.redis_url_value, socket_connect_timeout=2, socket_timeout=2)
    try:
        await client.ping()
    finally:
        await client.aclose()


async def get_schema_version() -> str:
    async with engine.connect() as connection:
        table_name = await connection.scalar(text("SELECT to_regclass('alembic_version')"))
        if table_name is None:
            return "unapplied"
        version = await connection.scalar(text("SELECT version_num FROM alembic_version LIMIT 1"))
        return str(version or "unapplied")
