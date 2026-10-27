import argparse
import asyncio
import getpass
import json
import os
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
from google_config_check import run_checks
from sqlalchemy import text
from sqlalchemy.engine import CursorResult

from app.core.config import settings
from app.db.session import AsyncSessionFactory

REGISTRY_DIRECTORY = Path(os.environ.get("ACCEPTANCE_REGISTRY_PATH", "/app/data/acceptance"))
REPORT_DIRECTORY = Path(os.environ.get("ACCEPTANCE_REPORT_PATH", "/app/data/acceptance-reports"))
API_URL = os.environ.get("ACCEPTANCE_API_URL", "http://127.0.0.1:8000").rstrip("/")
WORKSPACE_NAME = "Apps Script Bridge Acceptance"
CLEANUP_TABLES = [
    "sync_runs",
    "sync_conflicts",
    "sync_inbox",
    "sync_outbox",
    "google_sheet_bindings",
    "google_oauth_flows",
    "google_connections",
    "transaction_splits",
    "import_rows",
    "transactions",
    "import_batches",
    "categories",
    "accounts",
    "audit_log",
    "workspace_members",
    "auth_sessions",
    "workspaces",
    "users",
]
MANUAL_GATES = {
    "apps_script_configuration_reset",
    "apps_script_triggers_removed",
    "binding_deleted",
    "tunnel_stopped",
    "google_file_removed",
}
EVIDENCE_ITEMS = {
    "apps_script_package",
    "backup_restore",
    "backend_outage_recovery",
    "backend_change_pull",
    "binding",
    "conflict_keep_database",
    "conflict_keep_sheet",
    "conflict_manual_merge",
    "event_idempotency",
    "heartbeat",
    "hmac_replay",
    "initial_export",
    "lease",
    "pause_resume",
    "pull_ack",
    "pull_idempotency",
    "reconciliation",
    "register",
    "secret_rotation",
    "sheet_edit_update",
    "sheet_edit_push",
    "sheet_template",
    "technical_columns",
    "technical_tamper",
    "triggers",
}
OPTIONAL_EVIDENCE_ITEMS = {"oauth", "oauth_disconnect_revoke", "oauth_refresh"}
CREDENTIAL_NOTE_PATTERN = re.compile(
    r"(?:token|secret|password|oauth\s+code|client_secret|authorization|cookie|code=)",
    re.IGNORECASE,
)
JWT_NOTE_PATTERN = re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.")
LONG_SECRET_PATTERN = re.compile(r"[A-Za-z0-9_+/=-]{48,}")


class AcceptanceError(RuntimeError):
    pass


def _require_development() -> None:
    if settings.environment != "development" or settings.testing:
        raise AcceptanceError("Live acceptance commands run only in development")


def _registry_path(run_id: uuid.UUID) -> Path:
    return REGISTRY_DIRECTORY / f"{run_id}.json"


def _report_path(run_id: uuid.UUID) -> Path:
    return REPORT_DIRECTORY / f"google-live-acceptance-{run_id}.json"


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_registry(run_id: uuid.UUID) -> dict[str, Any]:
    path = _registry_path(run_id)
    if not path.is_file():
        raise AcceptanceError(f"Acceptance registry was not found for run {run_id}")
    registry = json.loads(path.read_text(encoding="utf-8"))
    if registry.get("acceptance_run_id") != str(run_id):
        raise AcceptanceError("Acceptance registry run ID is invalid")
    return registry


def _api_request(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = client.request(method, path, headers=headers, json=json_body)
    if response.status_code >= 400:
        try:
            code = response.json().get("error", {}).get("code", "API_ERROR")
        except ValueError:
            code = "API_ERROR"
        raise AcceptanceError(f"Backend request failed: {method} {path} ({code})")
    return response.json()


def _create_transaction(
    client: httpx.Client,
    headers: dict[str, str],
    *,
    transaction_type: str,
    amount: str,
    account_id: str,
    category_id: str | None = None,
    target_account_id: str | None = None,
    status: str = "confirmed",
) -> dict[str, Any]:
    return _api_request(
        client,
        "POST",
        "/api/v1/transactions",
        headers=headers,
        json_body={
            "occurred_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "transaction_type": transaction_type,
            "amount": amount,
            "currency": "RUB",
            "account_id": account_id,
            "target_account_id": target_account_id,
            "category_id": category_id,
            "status": status,
            "source": "manual",
            "comment": f"Acceptance run {headers['X-Acceptance-Run-ID']}",
            "splits": [],
        },
    )


def start() -> uuid.UUID:
    _require_development()
    checks = run_checks()
    for check in checks:
        print(f"{'PASS' if check.ok else 'FAIL'}: {check.name} — {check.message}")
    if not all(check.ok for check in checks):
        raise AcceptanceError("Google configuration check failed; no acceptance data was created")

    run_id = uuid.uuid4()
    acceptance_prefix = f"ASB-ACCEPT-{run_id.hex[:8].upper()}"
    email = f"acceptance-{run_id.hex}@example.invalid"
    password = getpass.getpass("Temporary acceptance user password (not stored): ")
    confirmation = getpass.getpass("Repeat temporary password: ")
    if password != confirmation or len(password) < 12:
        raise AcceptanceError("Passwords differ or contain fewer than 12 characters")

    with httpx.Client(base_url=API_URL, timeout=30) as client:
        registration = _api_request(
            client,
            "POST",
            "/api/v1/auth/register",
            json_body={
                "email": email,
                "display_name": f"{acceptance_prefix} Acceptance",
                "password": password,
                "workspace_name": WORKSPACE_NAME,
                "base_currency": "RUB",
                "timezone": "Asia/Yekaterinburg",
            },
        )
        workspace_id = str(registration["workspace"]["id"])
        user_id = str(registration["user"]["id"])
        headers = {
            "Authorization": f"Bearer {registration['access_token']}",
            "X-Workspace-ID": workspace_id,
            "X-Acceptance-Run-ID": str(run_id),
            "X-Acceptance-Prefix": acceptance_prefix,
        }
        registry = {
            "acceptance_run_id": str(run_id),
            "created_at": datetime.now(UTC).isoformat(),
            "state": "preparing",
            "user_id": user_id,
            "user_email": email,
            "workspace_id": workspace_id,
            "workspace_name": WORKSPACE_NAME,
            "acceptance_prefix": acceptance_prefix,
            "entity_ids": {"accounts": [], "categories": [], "transactions": []},
            "manual_gates": {name: False for name in sorted(MANUAL_GATES)},
            "evidence": {},
        }
        _write_json(_registry_path(run_id), registry)
        print(f"Acceptance run ID reserved: {run_id}")
        account_specs = [
            (f"{acceptance_prefix} ASB Тестовая карта", "debit_card"),
            (f"{acceptance_prefix} ASB Тестовые наличные", "cash"),
            (f"{acceptance_prefix} ASB Тестовый накопительный", "savings"),
        ]
        accounts = [
            _api_request(
                client,
                "POST",
                "/api/v1/accounts",
                headers=headers,
                json_body={
                    "name": name,
                    "account_type": account_type,
                    "currency": "RUB",
                    "opening_balance": "0",
                    "opening_balance_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
                },
            )
            for name, account_type in account_specs
        ]
        category_specs = [
            (f"{acceptance_prefix} ASB Тестовый доход", "income"),
            (f"{acceptance_prefix} ASB Тестовые продукты", "expense"),
            (f"{acceptance_prefix} ASB Тестовый транспорт", "expense"),
        ]
        categories = [
            _api_request(
                client,
                "POST",
                "/api/v1/categories",
                headers=headers,
                json_body={"name": name, "category_type": category_type},
            )
            for name, category_type in category_specs
        ]
        transactions = [
            _create_transaction(
                client,
                headers,
                transaction_type="income",
                amount="10000",
                account_id=str(accounts[0]["id"]),
                category_id=str(categories[0]["id"]),
            ),
            _create_transaction(
                client,
                headers,
                transaction_type="expense",
                amount="1200",
                account_id=str(accounts[0]["id"]),
                category_id=str(categories[1]["id"]),
            ),
            _create_transaction(
                client,
                headers,
                transaction_type="transfer",
                amount="2000",
                account_id=str(accounts[0]["id"]),
                target_account_id=str(accounts[2]["id"]),
            ),
            _create_transaction(
                client,
                headers,
                transaction_type="expense",
                amount="300",
                account_id=str(accounts[0]["id"]),
                category_id=str(categories[2]["id"]),
                status="draft",
            ),
        ]

    registry.update(
        {
            "state": "prepared",
            "entity_ids": {
                "accounts": [str(item["id"]) for item in accounts],
                "categories": [str(item["id"]) for item in categories],
                "transactions": [str(item["id"]) for item in transactions],
            },
            "prepared_at": datetime.now(UTC).isoformat(),
        }
    )
    _write_json(_registry_path(run_id), registry)
    print(f"Acceptance run ID: {run_id}")
    print(f"Acceptance login email: {email}")
    print("Password, access token and refresh cookie were not stored or printed")
    print("Next: log in through the browser and complete Apps Script Bridge acceptance")
    return run_id


def mark(run_id: uuid.UUID, item: str, status: str, note: str | None) -> None:
    _require_development()
    registry = _load_registry(run_id)
    if item not in MANUAL_GATES | EVIDENCE_ITEMS | OPTIONAL_EVIDENCE_ITEMS:
        raise AcceptanceError("Unknown acceptance evidence item")
    if note and (
        len(note) > 300
        or CREDENTIAL_NOTE_PATTERN.search(note)
        or JWT_NOTE_PATTERN.search(note)
        or LONG_SECRET_PATTERN.search(note)
        or ("?" in note and "://" in note)
    ):
        raise AcceptanceError("Evidence note may not contain credential-like material")
    if item in MANUAL_GATES:
        registry["manual_gates"][item] = status == "passed"
    else:
        registry.setdefault("evidence", {})[item] = {
            "status": status,
            "note": note,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
    registry["updated_at"] = datetime.now(UTC).isoformat()
    _write_json(_registry_path(run_id), registry)
    print(f"Recorded {item}={status} for acceptance run {run_id}")


async def _snapshot(registry: dict[str, Any]) -> dict[str, Any]:
    workspace_id = registry["workspace_id"]
    async with AsyncSessionFactory() as session:
        workspace = (
            (
                await session.execute(
                    text("SELECT id, name FROM workspaces WHERE id = CAST(:id AS UUID)"),
                    {"id": workspace_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if workspace is None or workspace["name"] != WORKSPACE_NAME:
            raise AcceptanceError("Acceptance workspace identity does not match registry")
        counts: dict[str, int] = {}
        for table in (
            "accounts",
            "categories",
            "transactions",
            "google_connections",
            "google_sheet_bindings",
            "sync_outbox",
            "sync_inbox",
            "sync_conflicts",
            "sync_runs",
            "audit_log",
        ):
            counts[table] = int(
                await session.scalar(
                    text(f"SELECT count(*) FROM {table} WHERE workspace_id = CAST(:id AS UUID)"),
                    {"id": workspace_id},
                )
                or 0
            )
        connection = (
            (
                await session.execute(
                    text(
                        "SELECT status, google_email, granted_scopes, revoked_at, "
                        "google_subject IS NOT NULL AS has_google_subject, "
                        "access_token_encrypted IS NOT NULL AS has_access_ciphertext, "
                        "refresh_token_encrypted IS NOT NULL AS has_refresh_ciphertext "
                        "FROM google_connections WHERE workspace_id = CAST(:id AS UUID) "
                        "ORDER BY updated_at DESC LIMIT 1"
                    ),
                    {"id": workspace_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        binding = (
            (
                await session.execute(
                    text(
                        "SELECT id, provider, spreadsheet_id, spreadsheet_url, spreadsheet_name, "
                        "template_version, status, sync_mode, last_successful_sync_at, "
                        "last_reconciliation_at, last_pull_at, last_ack_at, last_heartbeat_at, "
                        "binding_secret_hash IS NOT NULL AS has_binding_secret_hash "
                        "FROM google_sheet_bindings "
                        "WHERE workspace_id = CAST(:id AS UUID) ORDER BY updated_at DESC LIMIT 1"
                    ),
                    {"id": workspace_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        outbox = (
            (
                await session.execute(
                    text(
                        "SELECT status, count(*) AS count FROM sync_outbox "
                        "WHERE workspace_id = CAST(:id AS UUID) GROUP BY status ORDER BY status"
                    ),
                    {"id": workspace_id},
                )
            )
            .mappings()
            .all()
        )
        inbox = (
            (
                await session.execute(
                    text(
                        "SELECT status, count(*) AS count FROM sync_inbox "
                        "WHERE workspace_id = CAST(:id AS UUID) GROUP BY status ORDER BY status"
                    ),
                    {"id": workspace_id},
                )
            )
            .mappings()
            .all()
        )
        conflicts = (
            (
                await session.execute(
                    text(
                        "SELECT status, resolution, count(*) AS count FROM sync_conflicts "
                        "WHERE workspace_id = CAST(:id AS UUID) "
                        "GROUP BY status, resolution ORDER BY status, resolution"
                    ),
                    {"id": workspace_id},
                )
            )
            .mappings()
            .all()
        )
        sync_runs = (
            (
                await session.execute(
                    text(
                        "SELECT run_type, status, processed_count, conflict_count, error_count, "
                        "started_at, finished_at FROM sync_runs "
                        "WHERE workspace_id = CAST(:id AS UUID) ORDER BY started_at"
                    ),
                    {"id": workspace_id},
                )
            )
            .mappings()
            .all()
        )
    return {
        "counts": counts,
        "google_connection": dict(connection) if connection else None,
        "google_sheet_binding": dict(binding) if binding else None,
        "outbox_statuses": [dict(item) for item in outbox],
        "inbox_statuses": [dict(item) for item in inbox],
        "conflict_statuses": [dict(item) for item in conflicts],
        "sync_runs": [dict(item) for item in sync_runs],
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, uuid.UUID)):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


async def report(run_id: uuid.UUID) -> Path:
    _require_development()
    registry = _load_registry(run_id)
    snapshot = await _snapshot(registry)
    missing_evidence = [
        name
        for name in sorted(EVIDENCE_ITEMS)
        if registry.get("evidence", {}).get(name, {}).get("status") != "passed"
    ]
    report_value = _json_safe(
        {
            "acceptance_run_id": str(run_id),
            "generated_at": datetime.now(UTC),
            "registry": registry,
            "database_evidence": snapshot,
            "acceptance_complete": not missing_evidence,
            "missing_or_failed_evidence": missing_evidence,
            "optional_oauth_evidence": {
                name: registry.get("evidence", {}).get(name)
                for name in sorted(OPTIONAL_EVIDENCE_ITEMS)
            },
            "secrets_included": False,
            "limitations": [
                "Manual Google/UI assertions are authoritative only when marked passed.",
                "Report intentionally omits credentials, binding secrets, OAuth code, "
                "cookies and row payloads.",
            ],
        }
    )
    path = _report_path(run_id)
    _write_json(path, report_value)
    registry["state"] = "reported"
    registry["report_path"] = str(path)
    registry["updated_at"] = datetime.now(UTC).isoformat()
    _write_json(_registry_path(run_id), registry)
    print(f"Acceptance report written: {path}")
    return path


async def cleanup(run_id: uuid.UUID, *, confirmed: bool) -> None:
    _require_development()
    registry = _load_registry(run_id)
    if not confirmed:
        raise AcceptanceError("Cleanup requires confirmation of the exact acceptance run ID")
    if registry.get("state") != "reported" or not _report_path(run_id).is_file():
        raise AcceptanceError("Generate and preserve the acceptance report before cleanup")
    missing_gates = [
        name for name in sorted(MANUAL_GATES) if registry["manual_gates"].get(name) is not True
    ]
    if missing_gates:
        raise AcceptanceError(f"Manual cleanup gates are not confirmed: {', '.join(missing_gates)}")
    missing_evidence = [
        name
        for name in sorted(EVIDENCE_ITEMS)
        if registry.get("evidence", {}).get(name, {}).get("status") != "passed"
    ]
    if missing_evidence:
        raise AcceptanceError(f"Acceptance evidence is incomplete: {', '.join(missing_evidence)}")

    workspace_id = registry["workspace_id"]
    user_id = registry["user_id"]
    expected_email = registry["user_email"]
    if expected_email != f"acceptance-{run_id.hex}@example.invalid":
        raise AcceptanceError("Acceptance email does not match the exact run ID")
    try:
        uuid.UUID(str(workspace_id))
        uuid.UUID(str(user_id))
    except ValueError as exc:
        raise AcceptanceError("Acceptance registry contains an invalid object UUID") from exc
    async with AsyncSessionFactory() as session:
        identity = (
            (
                await session.execute(
                    text(
                        "SELECT w.name AS workspace_name, u.email, "
                        "(SELECT count(*) FROM workspace_members wm WHERE wm.user_id = u.id) "
                        "AS memberships FROM workspaces w JOIN users u ON u.id = w.owner_user_id "
                        "WHERE w.id = CAST(:workspace_id AS UUID) AND u.id = CAST(:user_id AS UUID)"
                    ),
                    {"workspace_id": workspace_id, "user_id": user_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            identity is None
            or identity["workspace_name"] != WORKSPACE_NAME
            or identity["email"] != expected_email
            or int(identity["memberships"]) != 1
        ):
            raise AcceptanceError(
                "Workspace/user identity does not exactly match acceptance registry"
            )
        active_connection = await session.scalar(
            text(
                "SELECT count(*) FROM google_connections "
                "WHERE workspace_id = CAST(:id AS UUID) AND status = 'active'"
            ),
            {"id": workspace_id},
        )
        if int(active_connection or 0) != 0:
            raise AcceptanceError("Revoke or disconnect the Google connection before cleanup")

    deleted_counts: dict[str, int] = {}
    async with AsyncSessionFactory() as session, session.begin():
        statements = [
            ("sync_runs", "DELETE FROM sync_runs WHERE workspace_id = CAST(:workspace_id AS UUID)"),
            (
                "sync_conflicts",
                "DELETE FROM sync_conflicts WHERE workspace_id = CAST(:workspace_id AS UUID)",
            ),
            (
                "sync_inbox",
                "DELETE FROM sync_inbox WHERE workspace_id = CAST(:workspace_id AS UUID)",
            ),
            (
                "sync_outbox",
                "DELETE FROM sync_outbox WHERE workspace_id = CAST(:workspace_id AS UUID)",
            ),
            (
                "google_sheet_bindings",
                "DELETE FROM google_sheet_bindings "
                "WHERE workspace_id = CAST(:workspace_id AS UUID)",
            ),
            (
                "google_oauth_flows",
                "DELETE FROM google_oauth_flows WHERE workspace_id = CAST(:workspace_id AS UUID)",
            ),
            (
                "google_connections",
                "DELETE FROM google_connections WHERE workspace_id = CAST(:workspace_id AS UUID)",
            ),
            (
                "transaction_splits",
                "DELETE FROM transaction_splits WHERE transaction_id IN "
                "(SELECT id FROM transactions "
                "WHERE workspace_id = CAST(:workspace_id AS UUID))",
            ),
            (
                "import_rows",
                "DELETE FROM import_rows WHERE batch_id IN "
                "(SELECT id FROM import_batches "
                "WHERE workspace_id = CAST(:workspace_id AS UUID))",
            ),
            (
                "transactions",
                "DELETE FROM transactions WHERE workspace_id = CAST(:workspace_id AS UUID)",
            ),
            (
                "import_batches",
                "DELETE FROM import_batches WHERE workspace_id = CAST(:workspace_id AS UUID)",
            ),
            (
                "categories",
                "DELETE FROM categories WHERE workspace_id = CAST(:workspace_id AS UUID)",
            ),
            ("accounts", "DELETE FROM accounts WHERE workspace_id = CAST(:workspace_id AS UUID)"),
            ("audit_log", "DELETE FROM audit_log WHERE workspace_id = CAST(:workspace_id AS UUID)"),
            (
                "workspace_members",
                "DELETE FROM workspace_members "
                "WHERE workspace_id = CAST(:workspace_id AS UUID) "
                "AND user_id = CAST(:user_id AS UUID)",
            ),
            ("auth_sessions", "DELETE FROM auth_sessions WHERE user_id = CAST(:user_id AS UUID)"),
            ("workspaces", "DELETE FROM workspaces WHERE id = CAST(:workspace_id AS UUID)"),
            ("users", "DELETE FROM users WHERE id = CAST(:user_id AS UUID) AND email = :email"),
        ]
        parameters = {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "email": expected_email,
        }
        for table, statement in statements:
            result = cast(
                CursorResult[Any],
                await session.execute(text(statement), parameters),
            )
            deleted_counts[table] = int(result.rowcount or 0)
    completed_at = datetime.now(UTC).isoformat()
    registry["state"] = "cleaned"
    registry["cleaned_at"] = completed_at
    registry["deleted_counts"] = deleted_counts
    report_value = json.loads(_report_path(run_id).read_text(encoding="utf-8"))
    report_value["cleanup"] = {
        "completed_at": completed_at,
        "deleted_counts": deleted_counts,
        "explicit_tables": CLEANUP_TABLES,
        "manual_gates": registry["manual_gates"],
        "registry_state": registry["state"],
        "user_data_outside_acceptance_run_touched": False,
    }
    _write_json(_report_path(run_id), report_value)
    _write_json(_registry_path(run_id), registry)
    print(f"Acceptance run {run_id} was cleaned using exact workspace/user IDs")


def _run_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("run ID must be a UUID") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Safe live Google acceptance registry and wizard")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("start")
    mark_parser = subparsers.add_parser("mark")
    mark_parser.add_argument("--run-id", required=True, type=_run_id)
    mark_parser.add_argument("--item", required=True)
    mark_parser.add_argument("--status", choices=("passed", "failed", "blocked"), required=True)
    mark_parser.add_argument("--note")
    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("--run-id", required=True, type=_run_id)
    cleanup_parser = subparsers.add_parser("cleanup")
    cleanup_parser.add_argument("--run-id", required=True, type=_run_id)
    cleanup_parser.add_argument("--yes", action="store_true")
    arguments = parser.parse_args()
    try:
        if arguments.command == "start":
            start()
        elif arguments.command == "mark":
            mark(arguments.run_id, arguments.item, arguments.status, arguments.note)
        elif arguments.command == "report":
            asyncio.run(report(arguments.run_id))
        else:
            confirmed = arguments.yes
            if not confirmed:
                typed = input(
                    f"Type the full acceptance run ID {arguments.run_id} to continue: "
                ).strip()
                confirmed = typed == str(arguments.run_id)
            asyncio.run(cleanup(arguments.run_id, confirmed=confirmed))
    except (AcceptanceError, httpx.HTTPError) as exc:
        print(f"GOOGLE LIVE ACCEPTANCE: {exc}", file=sys.stderr)
        raise SystemExit(5) from exc


if __name__ == "__main__":
    main()
