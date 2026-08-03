import asyncio
import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.test_database_safety import (
    DatabaseSafetyError,
    assert_test_database_marker,
    validate_test_database_target,
)

try:
    database_url = os.environ["DATABASE_URL"]
    test_run_id = os.environ.get("TEST_RUN_ID")
    validate_test_database_target(
        database_url,
        environ=os.environ,
        expected_run_id=test_run_id,
    )
    asyncio.run(assert_test_database_marker(database_url, expected_run_id=test_run_id))
except (KeyError, DatabaseSafetyError) as exc:
    raise pytest.UsageError(f"TEST DATABASE SAFETY: {exc}") from exc

from app.main import app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
