import argparse
import asyncio
import json
import os
import sys
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.test_database_safety import (
    TEST_MARKER_KEY,
    DatabaseSafetyError,
    validate_test_database_target,
)


async def seed_marker(database_url: str, test_run_id: str) -> None:
    validate_test_database_target(
        database_url,
        environ=os.environ,
        expected_run_id=test_run_id,
    )
    engine = create_async_engine(database_url, poolclass=NullPool)
    marker = json.dumps({"testing": True, "test_run_id": test_run_id})
    try:
        async with engine.begin() as connection:
            table_exists = await connection.scalar(
                text("SELECT to_regclass('public.system_metadata') IS NOT NULL")
            )
            if not table_exists:
                raise DatabaseSafetyError(
                    "system_metadata does not exist; apply guarded migrations first"
                )
            await connection.execute(
                text(
                    "INSERT INTO system_metadata (id, key, value) "
                    "VALUES (CAST(:id AS UUID), :key, CAST(:value AS JSONB)) "
                    "ON CONFLICT (key) DO UPDATE "
                    "SET value = EXCLUDED.value, updated_at = now()"
                ),
                {"id": str(uuid.uuid4()), "key": TEST_MARKER_KEY, "value": marker},
            )
    finally:
        await engine.dispose()
    print("Test database marker written for the explicit test run")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Write the marker for one runner-created test database."
    )
    parser.add_argument("--test-run-id", required=True)
    arguments = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL", "")
    try:
        asyncio.run(seed_marker(database_url, arguments.test_run_id))
    except DatabaseSafetyError as exc:
        print(f"TEST DATABASE SAFETY: {exc}", file=sys.stderr)
        raise SystemExit(4) from exc


if __name__ == "__main__":
    main()
