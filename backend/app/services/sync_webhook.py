import hashlib
import hmac
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ApiError
from app.db.models.accounts import Account
from app.db.models.categories import Category
from app.db.models.google_sync import GoogleSheetBinding, SyncConflict, SyncInbox
from app.db.models.transactions import FinancialTransaction
from app.db.models.users import User, Workspace
from app.dependencies.context import RequestContext
from app.repositories import transactions as transaction_repository
from app.schemas.accounts import AccountUpdate
from app.schemas.categories import CategoryUpdate
from app.schemas.google import WebhookChangeRequest, WebhookChangeResponse
from app.schemas.transactions import TransactionCreate, TransactionUpdate
from app.services import accounts as account_service
from app.services import categories as category_service
from app.services import transactions as transaction_service
from app.services.audit import record_audit, snapshot
from app.services.calculations import calculate_balances
from app.services.sync_hash import canonical_value, row_hash
from app.services.sync_payload import (
    account_row,
    category_row,
    entity_payload,
    transaction_payload,
    transaction_row,
)

TECHNICAL_FIELDS = {
    "_id",
    "_workspace_id",
    "_account_id",
    "_target_account_id",
    "_category_id",
    "_version",
    "_row_hash",
    "_updated_at",
    "_deleted_at",
}
TYPE_VALUES = {
    "Доход": "income",
    "Расход": "expense",
    "Перевод": "transfer",
    "Возврат": "refund",
    "Корректировка": "adjustment",
}
STATUS_VALUES = {
    "Черновик": "draft",
    "Подтверждена": "confirmed",
    "Сверена": "reconciled",
    "Отменена": "cancelled",
}


async def authenticate_webhook(
    session: AsyncSession,
    *,
    binding_id: str | None,
    timestamp: str | None,
    nonce: str | None,
    signature: str | None,
    body: bytes,
) -> GoogleSheetBinding:
    if not all((binding_id, timestamp, nonce, signature)):
        raise ApiError(
            status_code=401,
            code="GOOGLE_WEBHOOK_SIGNATURE_INVALID",
            message="Webhook authentication headers are missing",
        )
    try:
        parsed_binding_id = uuid.UUID(str(binding_id))
        parsed_timestamp = int(str(timestamp))
    except ValueError as exc:
        raise ApiError(
            status_code=401,
            code="GOOGLE_WEBHOOK_SIGNATURE_INVALID",
            message="Webhook authentication headers are invalid",
        ) from exc
    if (
        abs(int(time.time()) - parsed_timestamp)
        > settings.google_sheets_webhook_max_clock_skew_seconds
    ):
        raise ApiError(
            status_code=401,
            code="GOOGLE_WEBHOOK_EXPIRED",
            message="Webhook timestamp is outside the allowed window",
        )
    binding = await session.scalar(
        select(GoogleSheetBinding).where(
            GoogleSheetBinding.id == parsed_binding_id,
            GoogleSheetBinding.deleted_at.is_(None),
        )
    )
    if binding is None:
        raise ApiError(
            status_code=404, code="GOOGLE_SHEET_NOT_FOUND", message="Binding was not found"
        )
    if not binding.webhook_secret_hash or not binding.apps_script_enabled:
        raise ApiError(
            status_code=503,
            code="GOOGLE_WEBHOOK_NOT_CONFIGURED",
            message="Apps Script webhook is not configured",
        )
    if not binding.sync_enabled or binding.sync_mode != "bidirectional":
        raise ApiError(
            status_code=409,
            code="GOOGLE_SYNC_PAUSED",
            message="Incoming synchronization is disabled",
        )
    body_hash = hashlib.sha256(body).hexdigest()
    signed = f"{parsed_timestamp}\n{nonce}\n{body_hash}".encode()
    expected = hmac.new(
        bytes.fromhex(binding.webhook_secret_hash), signed, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, str(signature).lower()):
        raise ApiError(
            status_code=401,
            code="GOOGLE_WEBHOOK_SIGNATURE_INVALID",
            message="Webhook signature is invalid",
        )
    redis = Redis.from_url(settings.redis_url_value, decode_responses=True)
    try:
        accepted = await redis.set(
            f"google:webhook:nonce:{binding.id}:{nonce}",
            "1",
            ex=settings.google_sheets_webhook_max_clock_skew_seconds * 2,
            nx=True,
        )
    finally:
        await redis.aclose()
    if not accepted:
        raise ApiError(
            status_code=409,
            code="GOOGLE_WEBHOOK_REPLAY_DETECTED",
            message="Webhook nonce has already been used",
        )
    return binding


def _value(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in data and data[name] not in (None, ""):
            return data[name]
    return None


async def _resolve_account(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    data: dict[str, Any],
    *,
    target: bool = False,
) -> uuid.UUID | None:
    id_names = (
        ("target_account_id", "_target_account_id")
        if target
        else (
            "account_id",
            "_account_id",
        )
    )
    name_names = ("target_account", "Счёт назначения") if target else ("account", "Счёт")
    raw_id = _value(data, *id_names)
    if raw_id:
        try:
            account_id = uuid.UUID(str(raw_id))
        except ValueError as exc:
            raise ApiError(
                status_code=422, code="VALIDATION_ERROR", message="Account UUID is invalid"
            ) from exc
        account = await session.scalar(
            select(Account).where(
                Account.id == account_id,
                Account.workspace_id == workspace_id,
                Account.deleted_at.is_(None),
            )
        )
        if account is None:
            raise ApiError(status_code=422, code="ACCOUNT_NOT_FOUND", message="Account is unknown")
        return account.id
    raw_name = _value(data, *name_names)
    if not raw_name:
        return None
    matches = list(
        (
            await session.scalars(
                select(Account).where(
                    Account.workspace_id == workspace_id,
                    Account.deleted_at.is_(None),
                    func.lower(Account.name) == str(raw_name).strip().casefold(),
                )
            )
        ).all()
    )
    if len(matches) != 1:
        raise ApiError(
            status_code=422,
            code="ACCOUNT_NOT_FOUND",
            message="Account name is unknown or ambiguous",
        )
    return matches[0].id


async def _resolve_category(
    session: AsyncSession, workspace_id: uuid.UUID, data: dict[str, Any]
) -> uuid.UUID | None:
    raw_id = _value(data, "category_id", "_category_id")
    if raw_id:
        try:
            category_id = uuid.UUID(str(raw_id))
        except ValueError as exc:
            raise ApiError(
                status_code=422, code="VALIDATION_ERROR", message="Category UUID is invalid"
            ) from exc
        category = await session.scalar(
            select(Category).where(
                Category.id == category_id,
                Category.workspace_id == workspace_id,
                Category.deleted_at.is_(None),
            )
        )
        if category is None:
            raise ApiError(
                status_code=422, code="CATEGORY_NOT_FOUND", message="Category is unknown"
            )
        return category.id
    raw_name = _value(data, "category", "Категория")
    if not raw_name:
        return None
    matches = list(
        (
            await session.scalars(
                select(Category).where(
                    Category.workspace_id == workspace_id,
                    Category.deleted_at.is_(None),
                    func.lower(Category.name) == str(raw_name).strip().casefold(),
                )
            )
        ).all()
    )
    if len(matches) != 1:
        raise ApiError(
            status_code=422,
            code="CATEGORY_NOT_FOUND",
            message="Category name is unknown or ambiguous",
        )
    return matches[0].id


def _decimal(value: Any) -> str:
    normalized = str(value).strip().replace(" ", "").replace("\u00a0", "").replace("\u202f", "")
    if "," in normalized and "." in normalized:
        if normalized.rfind(",") > normalized.rfind("."):
            normalized = normalized.replace(".", "").replace(",", ".")
        else:
            normalized = normalized.replace(",", "")
    elif "," in normalized:
        normalized = normalized.replace(",", ".")
    try:
        amount = Decimal(normalized)
    except (InvalidOperation, ValueError) as exc:
        raise ApiError(
            status_code=422, code="VALIDATION_ERROR", message="Amount is invalid"
        ) from exc
    if not amount.is_finite() or amount <= 0:
        raise ApiError(status_code=422, code="VALIDATION_ERROR", message="Amount must be positive")
    return str(amount)


async def _transaction_data(
    session: AsyncSession,
    workspace: Workspace,
    payload: WebhookChangeRequest,
    *,
    existing: FinancialTransaction | None,
) -> dict[str, Any]:
    values = {**payload.visible_row, **payload.changed_fields}
    result: dict[str, Any] = {}
    occurred = _value(values, "occurred_at")
    if occurred is None:
        date_value = _value(values, "date", "Дата")
        time_value = _value(values, "time", "Время") or "00:00:00"
        if date_value:
            occurred = f"{date_value}T{time_value}"
    if occurred is not None:
        from datetime import datetime as dt
        from zoneinfo import ZoneInfo

        try:
            parsed = dt.fromisoformat(str(occurred).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ApiError(
                status_code=422, code="VALIDATION_ERROR", message="Date or time is invalid"
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(workspace.timezone))
        result["occurred_at"] = parsed.astimezone(UTC)
    transaction_type = _value(values, "transaction_type", "type", "Тип")
    if transaction_type is not None:
        result["transaction_type"] = TYPE_VALUES.get(str(transaction_type), str(transaction_type))
    amount = _value(values, "amount", "Сумма")
    if amount is not None:
        result["amount"] = _decimal(amount)
    currency = _value(values, "currency", "Валюта")
    if currency is not None:
        result["currency"] = str(currency).upper()
    account_id = await _resolve_account(session, workspace.id, values)
    if account_id is not None:
        result["account_id"] = account_id
    target_account_id = await _resolve_account(session, workspace.id, values, target=True)
    if target_account_id is not None or any(
        name in values
        for name in ("target_account_id", "_target_account_id", "target_account", "Счёт назначения")
    ):
        result["target_account_id"] = target_account_id
    category_id = await _resolve_category(session, workspace.id, values)
    if category_id is not None or any(
        name in values for name in ("category_id", "_category_id", "category", "Категория")
    ):
        result["category_id"] = category_id
    for field, aliases in {
        "counterparty": ("counterparty", "Контрагент"),
        "description": ("description", "Описание"),
        "comment": ("comment", "Комментарий"),
    }.items():
        raw = _value(values, *aliases)
        if raw is not None or any(alias in values for alias in aliases):
            result[field] = str(raw).strip() if raw is not None else None
    status = _value(values, "status", "Статус")
    if status is not None:
        result["status"] = STATUS_VALUES.get(str(status), str(status))
    if existing is None:
        result["source"] = "google_sheets"
        required = {"occurred_at", "transaction_type", "amount", "currency", "account_id"}
        missing = sorted(required - result.keys())
        if missing:
            raise ApiError(
                status_code=422,
                code="VALIDATION_ERROR",
                message="New Google Sheets transaction is incomplete",
                details={"missing_fields": missing},
            )
    return result


async def _normalized_row(
    session: AsyncSession, workspace: Workspace, user: User, item: FinancialTransaction
) -> tuple[dict[str, Any], str]:
    accounts = list(
        (await session.scalars(select(Account).where(Account.workspace_id == workspace.id))).all()
    )
    categories = list(
        (await session.scalars(select(Category).where(Category.workspace_id == workspace.id))).all()
    )
    row = transaction_row(
        item,
        timezone_name=workspace.timezone,
        account_names={account.id: account.name for account in accounts},
        category_names={category.id: category.name for category in categories},
        owner_name=user.display_name,
    )
    return {str(index): value for index, value in enumerate(row)}, str(row[24])


async def _duplicate_response(
    session: AsyncSession,
    binding: GoogleSheetBinding,
    inbox: SyncInbox,
    event_id: str,
) -> WebhookChangeResponse:
    """Return the canonical row for a retry whose first response was lost."""
    if inbox.entity_id is None:
        return WebhookChangeResponse(status="duplicate", event_id=event_id)
    workspace = await session.get(Workspace, binding.workspace_id)
    user = await session.get(User, binding.created_by)
    if workspace is None or user is None:
        return WebhookChangeResponse(
            status="duplicate",
            event_id=event_id,
            entity_id=inbox.entity_id,
        )
    if inbox.entity_type == "transaction":
        transaction = await transaction_repository.get_transaction(
            session,
            binding.workspace_id,
            inbox.entity_id,
            include_deleted=True,
        )
        if transaction is None:
            return WebhookChangeResponse(
                status="duplicate",
                event_id=event_id,
                entity_id=inbox.entity_id,
            )
        normalized, normalized_hash = await _normalized_row(session, workspace, user, transaction)
        return WebhookChangeResponse(
            status="duplicate",
            event_id=event_id,
            entity_id=transaction.id,
            version=transaction.version,
            row_hash=normalized_hash,
            normalized_row=normalized,
        )
    if inbox.entity_type == "account":
        account = await session.scalar(
            select(Account).where(
                Account.id == inbox.entity_id,
                Account.workspace_id == binding.workspace_id,
            )
        )
        if account is None:
            return WebhookChangeResponse(
                status="duplicate",
                event_id=event_id,
                entity_id=inbox.entity_id,
            )
        balances = {
            item.account_id: item.balance
            for item in await calculate_balances(session, binding.workspace_id)
        }
        row = account_row(account, calculated_balance=balances.get(account.id))
        return WebhookChangeResponse(
            status="duplicate",
            event_id=event_id,
            entity_id=account.id,
            version=account.version,
            row_hash=str(row[14]),
            normalized_row={str(index): value for index, value in enumerate(row)},
        )
    category = await session.scalar(
        select(Category).where(
            Category.id == inbox.entity_id,
            Category.workspace_id == binding.workspace_id,
        )
    )
    if category is None:
        return WebhookChangeResponse(
            status="duplicate",
            event_id=event_id,
            entity_id=inbox.entity_id,
        )
    parent = await session.get(Category, category.parent_id) if category.parent_id else None
    row = category_row(category, parent_name=parent.name if parent else None)
    return WebhookChangeResponse(
        status="duplicate",
        event_id=event_id,
        entity_id=category.id,
        version=category.version,
        row_hash=str(row[13]),
        normalized_row={str(index): value for index, value in enumerate(row)},
    )


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold()
    if normalized in {"да", "yes", "true", "1"}:
        return True
    if normalized in {"нет", "no", "false", "0", ""}:
        return False
    raise ApiError(status_code=422, code="VALIDATION_ERROR", message="Boolean value is invalid")


async def _reference_conflict(
    session: AsyncSession,
    binding: GoogleSheetBinding,
    inbox: SyncInbox,
    payload: WebhookChangeRequest,
    entity: Account | Category,
    request_id: str,
) -> WebhookChangeResponse:
    conflict = SyncConflict(
        workspace_id=binding.workspace_id,
        binding_id=binding.id,
        entity_type=payload.entity_type,
        entity_id=entity.id,
        database_version=entity.version,
        sheet_version=payload.expected_version,
        database_payload=canonical_value(entity_payload(payload.entity_type, entity)),
        sheet_payload=canonical_value(payload.model_dump(mode="json")),
        conflicting_fields=sorted(payload.changed_fields),
        status="open",
    )
    session.add(conflict)
    await session.flush()
    inbox.status = "conflict"
    inbox.entity_id = entity.id
    inbox.processed_at = datetime.now(UTC)
    await record_audit(
        session,
        workspace_id=binding.workspace_id,
        actor_user_id=None,
        entity_type="sync_conflict",
        entity_id=conflict.id,
        action="sync.conflict",
        before_data=None,
        after_data={"entity_id": str(entity.id), "fields": conflict.conflicting_fields},
        request_id=request_id,
        source="google_sheets",
    )
    await session.commit()
    return WebhookChangeResponse(
        status="conflict",
        event_id=payload.event_id,
        entity_id=entity.id,
        version=entity.version,
        row_hash=row_hash(entity_payload(payload.entity_type, entity)),
        conflict_id=conflict.id,
    )


async def _apply_reference_change(
    session: AsyncSession,
    binding: GoogleSheetBinding,
    inbox: SyncInbox,
    payload: WebhookChangeRequest,
    context: RequestContext,
) -> WebhookChangeResponse:
    if payload.entity_id is None:
        inbox.status = "rejected"
        inbox.last_error_code = "VALIDATION_ERROR"
        await session.commit()
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="New rows are currently supported only for transactions",
        )
    if payload.entity_type == "account":
        entity: Account | Category | None = await session.scalar(
            select(Account).where(
                Account.id == payload.entity_id,
                Account.workspace_id == binding.workspace_id,
                Account.deleted_at.is_(None),
            )
        )
    else:
        entity = await session.scalar(
            select(Category).where(
                Category.id == payload.entity_id,
                Category.workspace_id == binding.workspace_id,
                Category.deleted_at.is_(None),
            )
        )
    if entity is None:
        inbox.status = "rejected"
        inbox.last_error_code = "WORKSPACE_ACCESS_DENIED"
        await session.commit()
        raise ApiError(
            status_code=403,
            code="WORKSPACE_ACCESS_DENIED",
            message="Entity does not belong to the binding workspace",
        )
    if payload.expected_version != entity.version:
        return await _reference_conflict(
            session, binding, inbox, payload, entity, context.request_id
        )
    changes = dict(payload.changed_fields)
    try:
        if isinstance(entity, Account):
            forbidden = {
                "currency",
                "opening_balance",
                "opening_balance_at",
                "credit_limit",
                "description",
            }
            if forbidden & changes.keys():
                raise ApiError(
                    status_code=422,
                    code="VALIDATION_ERROR",
                    message="Currency and opening balance are read-only in Google Sheets",
                )
            allowed = {"name", "account_type", "institution", "is_archived"}
            update_values = {key: value for key, value in changes.items() if key in allowed}
            if "is_archived" in update_values:
                update_values["is_archived"] = _bool_value(update_values["is_archived"])
            entity = await account_service.update_account(
                session,
                context,
                entity.id,
                AccountUpdate.model_validate({**update_values, "version": entity.version}),
                commit=False,
                audit_source="google_sheets",
            )
            balances = {
                item.account_id: item.balance
                for item in await calculate_balances(session, binding.workspace_id)
            }
            normalized = account_row(entity, calculated_balance=balances.get(entity.id))
            normalized_hash = str(normalized[14])
        else:
            allowed = {
                "name",
                "category_type",
                "parent",
                "parent_id",
                "icon",
                "color",
                "sort_order",
                "is_archived",
            }
            update_values = {key: value for key, value in changes.items() if key in allowed}
            if "is_archived" in update_values:
                update_values["is_archived"] = _bool_value(update_values["is_archived"])
            if "sort_order" in update_values:
                update_values["sort_order"] = int(update_values["sort_order"] or 0)
            if "parent" in update_values:
                parent_name = str(update_values.pop("parent") or "").strip()
                if not parent_name:
                    update_values["parent_id"] = None
                else:
                    parents = list(
                        (
                            await session.scalars(
                                select(Category).where(
                                    Category.workspace_id == binding.workspace_id,
                                    Category.deleted_at.is_(None),
                                    func.lower(Category.name) == parent_name.casefold(),
                                )
                            )
                        ).all()
                    )
                    if len(parents) != 1:
                        raise ApiError(
                            status_code=422,
                            code="CATEGORY_NOT_FOUND",
                            message="Parent category is unknown or ambiguous",
                        )
                    update_values["parent_id"] = parents[0].id
            entity = await category_service.update_category(
                session,
                context,
                entity.id,
                CategoryUpdate.model_validate({**update_values, "version": entity.version}),
                commit=False,
                audit_source="google_sheets",
            )
            parent = await session.get(Category, entity.parent_id) if entity.parent_id else None
            normalized = category_row(entity, parent_name=parent.name if parent else None)
            normalized_hash = str(normalized[13])
    except (ApiError, ValueError) as exc:
        code = exc.code if isinstance(exc, ApiError) else "VALIDATION_ERROR"
        message = exc.message if isinstance(exc, ApiError) else "Field value is invalid"
        inbox.status = "rejected"
        inbox.validation_errors = [{"code": code, "message": message}]
        inbox.last_error_code = code
        inbox.processed_at = datetime.now(UTC)
        await session.commit()
        if isinstance(exc, ApiError):
            raise
        raise ApiError(status_code=422, code=code, message=message) from exc
    inbox.status = "applied"
    inbox.entity_id = entity.id
    inbox.processed_at = datetime.now(UTC)
    if binding.provider == "apps_script_bridge":
        binding.last_push_at = inbox.processed_at
    else:
        binding.last_pull_at = inbox.processed_at
    binding.last_successful_sync_at = inbox.processed_at
    await session.commit()
    return WebhookChangeResponse(
        status="applied",
        event_id=payload.event_id,
        entity_id=entity.id,
        version=entity.version,
        row_hash=normalized_hash,
        normalized_row={str(index): value for index, value in enumerate(normalized)},
    )


async def apply_change(
    session: AsyncSession,
    binding: GoogleSheetBinding,
    payload: WebhookChangeRequest,
    *,
    request_id: str,
) -> WebhookChangeResponse:
    if payload.spreadsheet_id != binding.spreadsheet_id:
        raise ApiError(
            status_code=403,
            code="WORKSPACE_ACCESS_DENIED",
            message="Spreadsheet does not match binding",
        )
    expected_sheet = {
        "transaction": "Операции",
        "account": "Счета",
        "category": "Категории",
    }[payload.entity_type]
    if payload.sheet_name != expected_sheet:
        raise ApiError(
            status_code=422, code="VALIDATION_ERROR", message="This sheet change is not supported"
        )
    changed_technical = sorted(TECHNICAL_FIELDS & payload.changed_fields.keys())
    if changed_technical:
        await record_audit(
            session,
            workspace_id=binding.workspace_id,
            actor_user_id=None,
            entity_type="google_sheet_binding",
            entity_id=binding.id,
            action="sync.error",
            before_data=None,
            after_data={"reason": "technical_field_tamper", "fields": changed_technical},
            request_id=request_id,
            source="google_sheets",
        )
        await session.commit()
        raise ApiError(
            status_code=422,
            code="GOOGLE_SHEET_TEMPLATE_INVALID",
            message="Technical fields are read-only",
        )
    idempotency_key = f"{binding.id}:{payload.event_id}"
    existing_inbox = await session.scalar(
        select(SyncInbox).where(SyncInbox.idempotency_key == idempotency_key)
    )
    if existing_inbox is not None and existing_inbox.status != "rejected":
        return await _duplicate_response(session, binding, existing_inbox, payload.event_id)
    if existing_inbox is not None:
        inbox = existing_inbox
        inbox.sheet_name = payload.sheet_name
        inbox.source_row_number = payload.row_number
        inbox.entity_type = payload.entity_type
        inbox.entity_id = payload.entity_id
        inbox.expected_version = payload.expected_version
        inbox.payload = canonical_value(payload.model_dump(mode="json"))
        inbox.row_hash = payload.row_hash
        inbox.status = "received"
        inbox.validation_errors = None
        inbox.last_error_code = None
        inbox.processed_at = None
    else:
        inbox = SyncInbox(
            workspace_id=binding.workspace_id,
            binding_id=binding.id,
            sheet_name=payload.sheet_name,
            source_event_id=payload.event_id,
            source_row_number=payload.row_number,
            entity_type=payload.entity_type,
            entity_id=payload.entity_id,
            expected_version=payload.expected_version,
            payload=canonical_value(payload.model_dump(mode="json")),
            row_hash=payload.row_hash,
            idempotency_key=idempotency_key,
            status="received",
        )
        session.add(inbox)
    await session.flush()
    workspace = await session.get(Workspace, binding.workspace_id)
    user = await session.get(User, binding.created_by)
    if workspace is None or user is None:
        raise ApiError(status_code=500, code="INTERNAL_ERROR", message="Binding context is invalid")
    context = RequestContext(user=user, workspace=workspace, role="owner", request_id=request_id)
    if payload.entity_type in {"account", "category"}:
        return await _apply_reference_change(session, binding, inbox, payload, context)
    transaction = None
    if payload.entity_id is not None:
        transaction = await transaction_repository.get_transaction(
            session,
            binding.workspace_id,
            payload.entity_id,
            include_deleted=True,
        )
        if transaction is None:
            inbox.status = "rejected"
            inbox.last_error_code = "TRANSACTION_NOT_FOUND"
            await session.commit()
            raise ApiError(
                status_code=404, code="TRANSACTION_NOT_FOUND", message="Transaction was not found"
            )
        if payload.expected_version != transaction.version:
            conflict = SyncConflict(
                workspace_id=binding.workspace_id,
                binding_id=binding.id,
                entity_type="transaction",
                entity_id=transaction.id,
                database_version=transaction.version,
                sheet_version=payload.expected_version,
                database_payload=canonical_value(snapshot("transaction", transaction)),
                sheet_payload=canonical_value(payload.model_dump(mode="json")),
                conflicting_fields=sorted(payload.changed_fields),
                status="open",
            )
            session.add(conflict)
            await session.flush()
            inbox.status = "conflict"
            inbox.entity_id = transaction.id
            inbox.processed_at = datetime.now(UTC)
            await record_audit(
                session,
                workspace_id=binding.workspace_id,
                actor_user_id=None,
                entity_type="sync_conflict",
                entity_id=conflict.id,
                action="sync.conflict",
                before_data=None,
                after_data={
                    "entity_id": str(transaction.id),
                    "fields": conflict.conflicting_fields,
                },
                request_id=request_id,
                source="google_sheets",
            )
            await session.commit()
            return WebhookChangeResponse(
                status="conflict",
                event_id=payload.event_id,
                entity_id=transaction.id,
                version=transaction.version,
                row_hash=row_hash(transaction_payload(transaction)),
                conflict_id=conflict.id,
            )
    try:
        values = await _transaction_data(
            session,
            workspace,
            payload,
            existing=transaction,
        )
        if transaction is None:
            transaction = await transaction_service.create_transaction(
                session,
                context,
                TransactionCreate.model_validate(values),
                commit=False,
                audit_source="google_sheets",
            )
        else:
            transaction = await transaction_service.update_transaction(
                session,
                context,
                transaction.id,
                TransactionUpdate.model_validate({**values, "version": transaction.version}),
                commit=False,
                audit_source="google_sheets",
            )
    except ApiError as exc:
        inbox.status = "rejected"
        inbox.validation_errors = [{"code": exc.code, "message": exc.message}]
        inbox.last_error_code = exc.code
        inbox.processed_at = datetime.now(UTC)
        await session.commit()
        raise
    inbox.status = "applied"
    inbox.entity_id = transaction.id
    inbox.processed_at = datetime.now(UTC)
    if binding.provider == "apps_script_bridge":
        binding.last_push_at = inbox.processed_at
    else:
        binding.last_pull_at = inbox.processed_at
    binding.last_successful_sync_at = inbox.processed_at
    await session.commit()
    normalized, normalized_hash = await _normalized_row(session, workspace, user, transaction)
    return WebhookChangeResponse(
        status="applied",
        event_id=payload.event_id,
        entity_id=transaction.id,
        version=transaction.version,
        row_hash=normalized_hash,
        normalized_row=normalized,
    )
