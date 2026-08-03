import os
import subprocess
import sys
import uuid

from sqlalchemy.engine import make_url

from app.core.test_database_safety import (
    DatabaseSafetyError,
    is_allowed_test_database_name,
    validate_test_database_target,
)


def _expect_guard(command: list[str], environment: dict[str, str], label: str) -> None:
    result = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode == 0 or "TEST DATABASE SAFETY" not in output:
        raise RuntimeError(
            f"{label} did not fail closed as expected "
            f"(exit={result.returncode}, output={output[-1200:]})"
        )
    print(f"PASS: {label} refused before database work")


def main() -> None:
    assert is_allowed_test_database_name("finspace_test")
    assert is_allowed_test_database_name("test_finspace")
    assert is_allowed_test_database_name(f"finspace_test_{uuid.uuid4().hex}")
    for name in ("finspace", "postgres", "production", "prod", "finance_dev"):
        assert not is_allowed_test_database_name(name)

    current = make_url(os.environ["DATABASE_URL"])
    dev_url = current.set(database="finspace").render_as_string(hide_password=False)
    test_url = current.set(database="finspace_test").render_as_string(hide_password=False)
    safe_environment = {**os.environ, "TESTING": "true", "ENVIRONMENT": "test"}
    try:
        validate_test_database_target(dev_url, environ=safe_environment)
    except DatabaseSafetyError:
        print("PASS: static guard rejected development database name")
    else:
        raise RuntimeError("Static guard accepted development database name")
    for environment in (
        {**safe_environment, "TESTING": "false"},
        {**safe_environment, "ENVIRONMENT": "production"},
    ):
        try:
            validate_test_database_target(test_url, environ=environment)
        except DatabaseSafetyError:
            pass
        else:
            raise RuntimeError("Static guard accepted a forbidden environment")
    print("PASS: TESTING and production guards are fail-closed")

    dev_environment = {
        **safe_environment,
        "DATABASE_URL": dev_url,
        "MIGRATION_TEST_CYCLE": "true",
    }
    _expect_guard(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/test_health.py"],
        dev_environment,
        "pytest with development DATABASE_URL",
    )
    _expect_guard(
        ["alembic", "current"],
        dev_environment,
        "migration test cycle with development DATABASE_URL",
    )
    _expect_guard(
        [
            sys.executable,
            "scripts/test_seed.py",
            "--test-run-id",
            str(uuid.uuid4()),
        ],
        dev_environment,
        "test seed with development DATABASE_URL",
    )
    _expect_guard(
        [
            sys.executable,
            "scripts/test_database_cleanup.py",
            "--database-url",
            dev_url,
            "--test-run-id",
            str(uuid.uuid4()),
            "--execute",
        ],
        dev_environment,
        "cleanup with development DATABASE_URL",
    )
    print("All test database safety checks passed")


if __name__ == "__main__":
    main()
