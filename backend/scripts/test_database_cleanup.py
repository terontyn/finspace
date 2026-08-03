import argparse
import asyncio
import os
import sys

import asyncpg
from sqlalchemy.engine import URL

from app.core.test_database_safety import (
    DatabaseSafetyError,
    assert_test_database_marker,
    validate_test_database_target,
)


def _render(url: URL) -> str:
    return url.render_as_string(hide_password=False)


async def cleanup(database_url: str, test_run_id: str, *, execute: bool) -> None:
    target = validate_test_database_target(
        database_url,
        environ=os.environ,
        expected_run_id=test_run_id,
    )
    if target.test_run_id is None:
        raise DatabaseSafetyError(
            "Cleanup is allowed only for finspace_test_<test_run_id> databases"
        )
    await assert_test_database_marker(database_url, expected_run_id=test_run_id)
    if not execute:
        print(f"Safety checks passed for {target.database_name}; no objects were deleted")
        return
    admin_url = target.url.set(drivername="postgresql", database="postgres")
    connection = await asyncpg.connect(_render(admin_url))
    try:
        await connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            target.database_name,
        )
        await connection.execute(f'DROP DATABASE "{target.database_name}"')
    finally:
        await connection.close()
    print(f"Removed isolated test database {target.database_name}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Drop one explicitly identified isolated Finspace test database."
    )
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--test-run-id", required=True)
    parser.add_argument("--execute", action="store_true")
    arguments = parser.parse_args()
    try:
        asyncio.run(
            cleanup(
                arguments.database_url,
                arguments.test_run_id,
                execute=arguments.execute,
            )
        )
    except DatabaseSafetyError as exc:
        print(f"TEST DATABASE SAFETY: {exc}", file=sys.stderr)
        raise SystemExit(4) from exc


if __name__ == "__main__":
    main()
