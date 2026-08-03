import asyncio
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

TEST_MARKER_KEY = "test_database_marker"
BANNED_DATABASE_NAMES = {"finspace", "postgres", "production", "prod"}
UNIQUE_TEST_DATABASE_PATTERN = re.compile(r"^finspace_test_([0-9a-f]{32})$")


class DatabaseSafetyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TestDatabaseTarget:
    url: URL
    database_name: str
    test_run_id: str | None


def _is_true(value: str | None) -> bool:
    return str(value or "").strip().casefold() == "true"


def is_allowed_test_database_name(database_name: str) -> bool:
    normalized = database_name.strip().casefold()
    if not normalized or normalized in BANNED_DATABASE_NAMES:
        return False
    return (
        normalized.endswith("_test")
        or normalized.startswith("test_")
        or UNIQUE_TEST_DATABASE_PATTERN.fullmatch(normalized) is not None
    )


def validate_test_database_target(
    database_url: str,
    *,
    environ: Mapping[str, str] | None = None,
    expected_run_id: str | None = None,
) -> TestDatabaseTarget:
    environment = environ if environ is not None else os.environ
    if not _is_true(environment.get("TESTING")):
        raise DatabaseSafetyError("TESTING=true is required for every test database command")
    if str(environment.get("ENVIRONMENT", "")).strip().casefold() == "production":
        raise DatabaseSafetyError("Test database commands are forbidden in production")
    try:
        parsed = make_url(database_url)
    except Exception as exc:
        raise DatabaseSafetyError("DATABASE_URL is invalid") from exc
    database_name = str(parsed.database or "")
    if not is_allowed_test_database_name(database_name):
        raise DatabaseSafetyError(
            "Refusing non-test database; expected *_test, test_* or finspace_test_<run_id>"
        )
    match = UNIQUE_TEST_DATABASE_PATTERN.fullmatch(database_name.casefold())
    run_id = match.group(1) if match else None
    normalized_expected = str(expected_run_id or "").replace("-", "").casefold() or None
    if normalized_expected is not None and run_id != normalized_expected:
        raise DatabaseSafetyError("Database name does not match the explicit test run ID")
    return TestDatabaseTarget(url=parsed, database_name=database_name, test_run_id=run_id)


async def read_test_database_marker(database_url: str) -> dict[str, Any] | None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            table_exists = await connection.scalar(
                text("SELECT to_regclass('public.system_metadata') IS NOT NULL")
            )
            if not table_exists:
                return None
            value = await connection.scalar(
                text("SELECT value FROM system_metadata WHERE key = :key"),
                {"key": TEST_MARKER_KEY},
            )
            return value if isinstance(value, dict) else None
    finally:
        await engine.dispose()


async def assert_test_database_marker(
    database_url: str,
    *,
    expected_run_id: str | None = None,
) -> dict[str, Any]:
    marker = await read_test_database_marker(database_url)
    if marker is None or marker.get("testing") is not True:
        raise DatabaseSafetyError(
            "Refusing test database command: test_database_marker is missing or invalid"
        )
    if expected_run_id is not None:
        marker_run_id = str(marker.get("test_run_id", "")).replace("-", "").casefold()
        normalized_expected = str(expected_run_id).replace("-", "").casefold()
        if marker_run_id != normalized_expected:
            raise DatabaseSafetyError("Test marker does not match the explicit test run ID")
    return marker


def assert_test_database_marker_sync(
    database_url: str,
    *,
    expected_run_id: str | None = None,
) -> dict[str, Any]:
    return asyncio.run(assert_test_database_marker(database_url, expected_run_id=expected_run_id))
