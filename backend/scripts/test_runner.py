import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import Sequence

import asyncpg
from sqlalchemy.engine import URL

from app.core.test_database_safety import (
    DatabaseSafetyError,
    assert_test_database_marker,
    validate_test_database_target,
)


def _render(url: URL) -> str:
    return url.render_as_string(hide_password=False)


def _admin_dsn(base_url: URL) -> str:
    return _render(base_url.set(drivername="postgresql", database="postgres"))


async def _create_database(base_url: URL, database_name: str) -> None:
    connection = await asyncpg.connect(_admin_dsn(base_url))
    try:
        await connection.execute(f'CREATE DATABASE "{database_name}"')
    finally:
        await connection.close()


async def _drop_database(
    base_url: URL,
    database_url: str,
    database_name: str,
    test_run_id: uuid.UUID,
    child_environment: dict[str, str],
) -> None:
    validate_test_database_target(
        database_url,
        environ=child_environment,
        expected_run_id=str(test_run_id),
    )
    await assert_test_database_marker(database_url, expected_run_id=str(test_run_id))
    connection = await asyncpg.connect(_admin_dsn(base_url))
    try:
        await connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database_name,
        )
        await connection.execute(f'DROP DATABASE "{database_name}"')
    finally:
        await connection.close()


def _run(command: Sequence[str], environment: dict[str, str]) -> None:
    subprocess.run(list(command), check=True, env=environment)


def _pytest_arguments(arguments: list[str]) -> list[str]:
    return arguments[1:] if arguments[:1] == ["--"] else arguments


async def run(arguments: list[str]) -> int:
    if os.environ.get("TESTING", "").casefold() != "true":
        raise DatabaseSafetyError("Runner requires TESTING=true")
    if os.environ.get("ENVIRONMENT", "").casefold() == "production":
        raise DatabaseSafetyError("Runner is forbidden in production")
    base_value = os.environ.get("TEST_DATABASE_URL", "")
    if not base_value:
        raise DatabaseSafetyError("TEST_DATABASE_URL is required")
    base_target = validate_test_database_target(
        base_value,
        environ={**os.environ, "TESTING": "true", "ENVIRONMENT": "test"},
    )
    test_run_id = uuid.uuid4()
    database_name = f"finspace_test_{test_run_id.hex}"
    database_url = _render(base_target.url.set(database=database_name))
    child_environment = {
        **os.environ,
        "DATABASE_URL": database_url,
        "ENVIRONMENT": "test",
        "TESTING": "true",
        "TEST_RUN_ID": str(test_run_id),
        "REDIS_URL": os.environ.get("TEST_REDIS_URL", "redis://redis:6379/15"),
        "ALLOW_DEV_AUTH_HEADERS": "false",
    }
    created = False
    cleanup_failed = False
    print(f"Test runner: creating isolated database {database_name}", flush=True)
    try:
        await _create_database(base_target.url, database_name)
        created = True
        _run(["alembic", "upgrade", "head"], child_environment)
        _run(
            ["python", "scripts/test_seed.py", "--test-run-id", str(test_run_id)],
            child_environment,
        )
        await assert_test_database_marker(database_url, expected_run_id=str(test_run_id))

        cycle_environment = {**child_environment, "MIGRATION_TEST_CYCLE": "true"}
        _run(["alembic", "downgrade", "0005_apps_script_bridge"], cycle_environment)
        _run(["alembic", "upgrade", "head"], cycle_environment)
        _run(["alembic", "downgrade", "0005_apps_script_bridge"], cycle_environment)
        _run(["alembic", "upgrade", "head"], cycle_environment)
        await assert_test_database_marker(database_url, expected_run_id=str(test_run_id))

        _run(
            ["python", "-m", "pytest", *_pytest_arguments(arguments)],
            child_environment,
        )
        return 0
    except subprocess.CalledProcessError as exc:
        return int(exc.returncode or 1)
    finally:
        if created:
            try:
                await _drop_database(
                    base_target.url,
                    database_url,
                    database_name,
                    test_run_id,
                    child_environment,
                )
                print(f"Test runner: removed isolated database {database_name}", flush=True)
            except Exception as exc:
                cleanup_failed = True
                print(
                    f"TEST DATABASE CLEANUP FAILED: {database_name} remains; {exc}",
                    file=sys.stderr,
                    flush=True,
                )
        if cleanup_failed:
            raise DatabaseSafetyError(
                f"Isolated test database {database_name} could not be removed"
            )


def main() -> None:
    try:
        raise SystemExit(asyncio.run(run(sys.argv[1:])))
    except DatabaseSafetyError as exc:
        print(f"TEST DATABASE SAFETY: {exc}", file=sys.stderr)
        raise SystemExit(4) from exc


if __name__ == "__main__":
    main()
