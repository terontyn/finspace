"""Ephemeral PostgreSQL proof for the v0.11 Stage A Payees migration.

This script is validation-branch evidence only. It creates and removes its own
databases on the GitHub Actions PostgreSQL service.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
ADMIN_DSN = os.environ.get(
    "PAYEES_VALIDATION_ADMIN_DSN",
    "postgresql://finspace:finspace@127.0.0.1:5432/postgres",
)
DATABASE_PREFIX = "finspace_validation_payees"


def _database_url(database: str) -> str:
    return f"postgresql+asyncpg://finspace:finspace@127.0.0.1:5432/{database}"


def _asyncpg_url(database: str) -> str:
    return f"postgresql://finspace:finspace@127.0.0.1:5432/{database}"


def _alembic(database: str, *arguments: str) -> str:
    environment = {
        **os.environ,
        "DATABASE_URL": _database_url(database),
        "ENVIRONMENT": "development",
        "TESTING": "false",
    }
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=BACKEND,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    if output:
        print(output, flush=True)
    return output


async def _replace_database(name: str) -> None:
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            name,
        )
        await connection.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await connection.execute(f'CREATE DATABASE "{name}"')
    finally:
        await connection.close()


async def _drop_database(name: str) -> None:
    connection = await asyncpg.connect(ADMIN_DSN)
    try:
        await connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            name,
        )
        await connection.execute(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        await connection.close()


async def _table_exists(connection: asyncpg.Connection, table: str) -> bool:
    return bool(
        await connection.fetchval(
            "SELECT to_regclass($1) IS NOT NULL",
            f"public.{table}",
        )
    )


async def _column_exists(
    connection: asyncpg.Connection,
    table: str,
    column: str,
) -> bool:
    return bool(
        await connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = $1 AND column_name = $2)",
            table,
            column,
        )
    )


async def _schema_inventory(connection: asyncpg.Connection) -> dict[str, object]:
    constraints = await connection.fetch(
        "SELECT conname, contype, pg_get_constraintdef(oid) AS definition "
        "FROM pg_constraint WHERE connamespace = 'public'::regnamespace "
        "AND (conrelid IN ('payees'::regclass, 'payee_aliases'::regclass, "
        "'transactions'::regclass, 'recurring_rules'::regclass)) "
        "AND (conname LIKE '%payee%' OR conrelid IN ('payees'::regclass, "
        "'payee_aliases'::regclass)) ORDER BY conname"
    )
    indexes = await connection.fetch(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' "
        "AND (tablename IN ('payees', 'payee_aliases') OR indexname LIKE '%payee%') "
        "ORDER BY indexname"
    )
    def serializable(row: asyncpg.Record) -> dict[str, object]:
        return {
            key: value.decode() if isinstance(value, bytes) else value
            for key, value in dict(row).items()
        }

    return {
        "constraints": [serializable(row) for row in constraints],
        "indexes": [serializable(row) for row in indexes],
    }


async def _assert_stage_a_schema(connection: asyncpg.Connection) -> None:
    assert await _table_exists(connection, "payees")
    assert await _table_exists(connection, "payee_aliases")
    assert await _column_exists(connection, "transactions", "payee_id")
    assert await _column_exists(connection, "recurring_rules", "payee_id")

    constraint_rows = await connection.fetch(
        "SELECT conname FROM pg_constraint "
        "WHERE connamespace = 'public'::regnamespace"
    )
    names = {row["conname"] for row in constraint_rows}
    required = {
        "uq_payees_id_workspace",
        "uq_payee_aliases_workspace_hash",
        "fk_payee_aliases_payee_workspace",
        "fk_transactions_payee_workspace",
        "fk_recurring_rules_payee_workspace",
    }
    assert required <= names, required - names
    assert any(name.endswith("ck_payee_aliases_primary_not_deleted") for name in names)

    primary_index = await connection.fetchval(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' "
        "AND indexname = 'uq_payee_aliases_active_primary'"
    )
    assert primary_index is not None
    assert "UNIQUE" in primary_index
    assert "is_primary = true" in primary_index


async def _assert_stage_a_absent(connection: asyncpg.Connection) -> None:
    assert not await _table_exists(connection, "payees")
    assert not await _table_exists(connection, "payee_aliases")
    assert not await _column_exists(connection, "transactions", "payee_id")
    assert not await _column_exists(connection, "recurring_rules", "payee_id")


async def _fresh_install_proof(database: str) -> None:
    print("proof=fresh_install phase=create", flush=True)
    await _replace_database(database)
    _alembic(database, "upgrade", "head")
    current = _alembic(database, "current")
    assert "0011_payees" in current

    connection = await asyncpg.connect(_asyncpg_url(database))
    try:
        await _assert_stage_a_schema(connection)
        print(
            "constraint_inventory=" + json.dumps(await _schema_inventory(connection), sort_keys=True),
            flush=True,
        )
    finally:
        await connection.close()

    _alembic(database, "downgrade", "0010_goals")
    connection = await asyncpg.connect(_asyncpg_url(database))
    try:
        await _assert_stage_a_absent(connection)
    finally:
        await connection.close()

    _alembic(database, "upgrade", "head")
    connection = await asyncpg.connect(_asyncpg_url(database))
    try:
        await _assert_stage_a_schema(connection)
    finally:
        await connection.close()
    print("proof=fresh_install result=PASS", flush=True)


async def _seed_historical_rows(connection: asyncpg.Connection) -> None:
    values = {
        "user_a": uuid.UUID("11111111-1111-4111-8111-111111111111"),
        "user_b": uuid.UUID("22222222-2222-4222-8222-222222222222"),
        "workspace_a": uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        "workspace_b": uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        "account_a": uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001"),
        "account_b": uuid.UUID("bbbbbbbb-0000-4000-8000-000000000001"),
        "category_a": uuid.UUID("aaaaaaaa-0000-4000-8000-000000000002"),
        "category_b": uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002"),
        "transaction_a": uuid.UUID("aaaaaaaa-0000-4000-8000-000000000003"),
        "transaction_b": uuid.UUID("bbbbbbbb-0000-4000-8000-000000000003"),
        "rule_a": uuid.UUID("aaaaaaaa-0000-4000-8000-000000000004"),
        "rule_b": uuid.UUID("bbbbbbbb-0000-4000-8000-000000000004"),
    }
    await connection.executemany(
        "INSERT INTO users (id, email, normalized_email, display_name) VALUES ($1, $2, $2, $3)",
        [
            (values["user_a"], "historical-a@example.test", "Historical A"),
            (values["user_b"], "historical-b@example.test", "Historical B"),
        ],
    )
    await connection.executemany(
        "INSERT INTO workspaces (id, name, base_currency, timezone, owner_user_id) "
        "VALUES ($1, $2, 'RUB', 'Asia/Yekaterinburg', $3)",
        [
            (values["workspace_a"], "Historical A", values["user_a"]),
            (values["workspace_b"], "Historical B", values["user_b"]),
        ],
    )
    await connection.executemany(
        "INSERT INTO accounts (id, workspace_id, name, account_type, currency, "
        "opening_balance_at) VALUES ($1, $2, $3, 'debit_card', 'RUB', now())",
        [
            (values["account_a"], values["workspace_a"], "Historical Card A"),
            (values["account_b"], values["workspace_b"], "Historical Card B"),
        ],
    )
    await connection.executemany(
        "INSERT INTO categories (id, workspace_id, name, category_type) "
        "VALUES ($1, $2, $3, 'expense')",
        [
            (values["category_a"], values["workspace_a"], "Historical Category A"),
            (values["category_b"], values["workspace_b"], "Historical Category B"),
        ],
    )
    await connection.executemany(
        "INSERT INTO transactions (id, workspace_id, occurred_at, transaction_type, amount, "
        "currency, account_id, category_id, counterparty, created_by, updated_by) "
        "VALUES ($1, $2, now(), 'expense', 10, 'RUB', $3, $4, $5, $6, $6)",
        [
            (
                values["transaction_a"],
                values["workspace_a"],
                values["account_a"],
                values["category_a"],
                "Historical Counterparty A",
                values["user_a"],
            ),
            (
                values["transaction_b"],
                values["workspace_b"],
                values["account_b"],
                values["category_b"],
                "Historical Counterparty B",
                values["user_b"],
            ),
        ],
    )
    await connection.executemany(
        "INSERT INTO recurring_rules (id, workspace_id, name, rule_type, schedule_rrule, "
        "timezone, transaction_type, amount, currency, account_id, category_id, "
        "counterparty, created_by) VALUES ($1, $2, $3, 'expense', 'FREQ=MONTHLY;BYMONTHDAY=1', "
        "'Asia/Yekaterinburg', 'expense', 20, 'RUB', $4, $5, $6, $7)",
        [
            (
                values["rule_a"],
                values["workspace_a"],
                "Historical Rule A",
                values["account_a"],
                values["category_a"],
                "Historical Recurring A",
                values["user_a"],
            ),
            (
                values["rule_b"],
                values["workspace_b"],
                "Historical Rule B",
                values["account_b"],
                values["category_b"],
                "Historical Recurring B",
                values["user_b"],
            ),
        ],
    )


async def _historical_snapshot(connection: asyncpg.Connection) -> dict[str, object]:
    transactions = await connection.fetch(
        "SELECT id::text, workspace_id::text, counterparty FROM transactions "
        "WHERE id IN ('aaaaaaaa-0000-4000-8000-000000000003', "
        "'bbbbbbbb-0000-4000-8000-000000000003') ORDER BY id"
    )
    rules = await connection.fetch(
        "SELECT id::text, workspace_id::text, counterparty FROM recurring_rules "
        "WHERE id IN ('aaaaaaaa-0000-4000-8000-000000000004', "
        "'bbbbbbbb-0000-4000-8000-000000000004') ORDER BY id"
    )
    return {
        "transactions": [dict(row) for row in transactions],
        "recurring_rules": [dict(row) for row in rules],
    }


async def _historical_upgrade_proof(database: str) -> None:
    print("proof=historical_upgrade phase=create", flush=True)
    await _replace_database(database)
    _alembic(database, "upgrade", "0010_goals")
    connection = await asyncpg.connect(_asyncpg_url(database))
    try:
        await _seed_historical_rows(connection)
        before = await _historical_snapshot(connection)
    finally:
        await connection.close()

    _alembic(database, "upgrade", "head")
    connection = await asyncpg.connect(_asyncpg_url(database))
    try:
        await _assert_stage_a_schema(connection)
        assert await _historical_snapshot(connection) == before
        assert await connection.fetchval("SELECT count(*) FROM payees") == 0
        assert await connection.fetchval("SELECT count(*) FROM payee_aliases") == 0
        assert await connection.fetchval(
            "SELECT count(*) FROM transactions WHERE payee_id IS NOT NULL"
        ) == 0
        assert await connection.fetchval(
            "SELECT count(*) FROM recurring_rules WHERE payee_id IS NOT NULL"
        ) == 0
    finally:
        await connection.close()

    _alembic(database, "downgrade", "0010_goals")
    connection = await asyncpg.connect(_asyncpg_url(database))
    try:
        await _assert_stage_a_absent(connection)
        assert await _historical_snapshot(connection) == before
    finally:
        await connection.close()

    _alembic(database, "upgrade", "head")
    connection = await asyncpg.connect(_asyncpg_url(database))
    try:
        await _assert_stage_a_schema(connection)
        assert await _historical_snapshot(connection) == before
        assert await connection.fetchval("SELECT count(*) FROM payees") == 0
        assert await connection.fetchval("SELECT count(*) FROM payee_aliases") == 0
    finally:
        await connection.close()
    print(
        "historical_snapshot=" + json.dumps(before, sort_keys=True),
        flush=True,
    )
    print("proof=historical_upgrade no_backfill=yes no_dml_effect=yes result=PASS", flush=True)


async def main() -> None:
    suffix = uuid.uuid4().hex[:10]
    fresh = f"{DATABASE_PREFIX}_fresh_{suffix}"
    historical = f"{DATABASE_PREFIX}_historical_{suffix}"
    try:
        await _fresh_install_proof(fresh)
        await _historical_upgrade_proof(historical)
        print("payees_postgres_migration_proof=PASS", flush=True)
    finally:
        await _drop_database(fresh)
        await _drop_database(historical)


if __name__ == "__main__":
    asyncio.run(main())
