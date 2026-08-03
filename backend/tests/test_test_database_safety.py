import os
import uuid

import pytest

from app.core.test_database_safety import (
    DatabaseSafetyError,
    assert_test_database_marker,
    is_allowed_test_database_name,
    validate_test_database_target,
)


def test_static_database_name_and_environment_guards() -> None:
    run_id = uuid.uuid4()
    safe_url = f"postgresql+asyncpg://user:password@postgres/finspace_test_{run_id.hex}"
    environment = {"TESTING": "true", "ENVIRONMENT": "test"}
    target = validate_test_database_target(
        safe_url,
        environ=environment,
        expected_run_id=str(run_id),
    )
    assert target.test_run_id == run_id.hex
    assert is_allowed_test_database_name("finspace_test")
    assert is_allowed_test_database_name("test_finspace")
    for database_name in ("finspace", "postgres", "production", "prod", "development"):
        assert not is_allowed_test_database_name(database_name)
    with pytest.raises(DatabaseSafetyError):
        validate_test_database_target(
            "postgresql+asyncpg://user:password@postgres/finspace",
            environ=environment,
        )
    with pytest.raises(DatabaseSafetyError):
        validate_test_database_target(safe_url, environ={**environment, "TESTING": "false"})
    with pytest.raises(DatabaseSafetyError):
        validate_test_database_target(
            safe_url,
            environ={**environment, "ENVIRONMENT": "production"},
        )


@pytest.mark.asyncio
async def test_runner_database_contains_matching_marker() -> None:
    marker = await assert_test_database_marker(
        os.environ["DATABASE_URL"],
        expected_run_id=os.environ["TEST_RUN_ID"],
    )
    assert marker["testing"] is True
    assert str(marker["test_run_id"]) == os.environ["TEST_RUN_ID"]
