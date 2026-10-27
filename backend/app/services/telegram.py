import hashlib
import hmac
import re
import secrets
import string
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models.accounts import Account
from app.db.models.automations import (
    RecurringRule,
    TelegramIntent,
    TelegramLink,
    TelegramLinkCode,
)
from app.db.models.categories import Category
from app.db.models.transactions import FinancialTransaction
from app.db.models.users import User, Workspace, WorkspaceMember
from app.dependencies.context import RequestContext
from app.schemas.automations import (
    TelegramButton,
    TelegramCallbackRequest,
    TelegramIntegrationResponse,
    TelegramLinkRequest,
    TelegramLinkStatusResponse,
    TelegramMessageRequest,
)
from app.schemas.transactions import TransactionCreate
from app.services import transactions
from app.services.audit import record_audit

LINK_CODE_TTL = timedelta(minutes=10)
INTENT_TTL = timedelta(minutes=15)
MAX_LINK_ATTEMPTS = 5
COMMANDS = {
    "/start": "Финпространство готово. Используйте /link <код> для привязки.",
    "/help": (
        "Команды: /today, /week, /month, /accounts, /uncategorized, /recurring. "
        "Операция: расход 1250 продукты основная карта."
    ),
}


@dataclass(slots=True)
class Ambiguity:
    selection_type: str
    candidates: list[tuple[uuid.UUID, str]]
    message: str


async def create_link_code(
    session: AsyncSession, context: RequestContext
) -> tuple[str, TelegramLinkCode]:
    now = datetime.now(UTC)
    previous = list(
        (
            await session.scalars(
                select(TelegramLinkCode).where(
                    TelegramLinkCode.user_id == context.user.id,
                    TelegramLinkCode.workspace_id == context.workspace.id,
                    TelegramLinkCode.used_at.is_(None),
                )
            )
        ).all()
    )
    for item in previous:
        item.used_at = now
    alphabet = string.ascii_uppercase + string.digits
    code = "".join(secrets.choice(alphabet) for _ in range(4))
    code += "-" + "".join(secrets.choice(alphabet) for _ in range(4))
    normalized = _normalize_code(code)
    row = TelegramLinkCode(
        code_hash=_hash(normalized),
        code_prefix=normalized[:4],
        user_id=context.user.id,
        workspace_id=context.workspace.id,
        expires_at=now + LINK_CODE_TTL,
        attempts=0,
        created_at=now,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return code, row


async def link(
    session: AsyncSession,
    data: TelegramLinkRequest,
    *,
    service_workspace_id: uuid.UUID | None,
    request_id: str,
) -> TelegramIntegrationResponse:
    normalized = _normalize_code(data.code)
    candidates = list(
        (
            await session.scalars(
                select(TelegramLinkCode).where(
                    TelegramLinkCode.code_prefix == normalized[:4],
                    TelegramLinkCode.used_at.is_(None),
                )
            )
        ).all()
    )
    code = next(
        (item for item in candidates if hmac.compare_digest(item.code_hash, _hash(normalized))),
        None,
    )
    if code is None:
        for item in candidates:
            item.attempts += 1
            if item.attempts >= MAX_LINK_ATTEMPTS:
                item.used_at = datetime.now(UTC)
        await session.flush()
        raise ApiError(
            status_code=401,
            code="TELEGRAM_LINK_CODE_INVALID",
            message="Telegram link code is invalid",
        )
    if service_workspace_id is not None and code.workspace_id != service_workspace_id:
        raise ApiError(
            status_code=403,
            code="WORKSPACE_ACCESS_DENIED",
            message="Service account cannot link a different workspace",
        )
    now = datetime.now(UTC)
    if code.attempts >= MAX_LINK_ATTEMPTS or code.expires_at <= now:
        code.used_at = now
        await session.flush()
        raise ApiError(
            status_code=410,
            code="TELEGRAM_LINK_CODE_EXPIRED",
            message="Telegram link code has expired",
        )
    member = await session.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.user_id == code.user_id,
            WorkspaceMember.workspace_id == code.workspace_id,
        )
    )
    if member is None:
        raise ApiError(
            status_code=403,
            code="WORKSPACE_ACCESS_DENIED",
            message="Link code owner no longer belongs to this workspace",
        )
    same_telegram = await session.scalar(
        select(TelegramLink).where(TelegramLink.telegram_user_id == data.telegram_user_id)
    )
    if same_telegram is not None and same_telegram.user_id != code.user_id:
        raise ApiError(
            status_code=409,
            code="TELEGRAM_LINK_CODE_INVALID",
            message="Telegram user is already linked to another user",
        )
    active_for_user = await session.scalar(
        select(TelegramLink).where(
            TelegramLink.user_id == code.user_id,
            TelegramLink.status == "active",
        )
    )
    if active_for_user is not None and active_for_user.telegram_user_id != data.telegram_user_id:
        raise ApiError(
            status_code=409,
            code="TELEGRAM_LINK_CODE_INVALID",
            message="User already has another active Telegram link",
        )
    link_row = same_telegram or active_for_user
    if link_row is None:
        link_row = TelegramLink(
            user_id=code.user_id,
            workspace_id=code.workspace_id,
            telegram_user_id=data.telegram_user_id,
            telegram_chat_id=data.telegram_chat_id,
            telegram_username=data.telegram_username,
            status="active",
            linked_at=now,
            last_seen_at=now,
        )
        session.add(link_row)
    else:
        link_row.workspace_id = code.workspace_id
        link_row.telegram_chat_id = data.telegram_chat_id
        link_row.telegram_username = data.telegram_username
        link_row.status = "active"
        link_row.revoked_at = None
        link_row.last_seen_at = now
    code.used_at = now
    await session.flush()
    await record_audit(
        session,
        workspace_id=code.workspace_id,
        actor_user_id=code.user_id,
        entity_type="telegram_link",
        entity_id=link_row.id,
        action="telegram.link",
        before_data=None,
        after_data={"telegram_user_id": str(data.telegram_user_id), "status": "active"},
        request_id=request_id,
        source="telegram",
    )
    return TelegramIntegrationResponse(
        status="linked",
        response_type="linked",
        messages=["Telegram успешно подключён к Финпространству."],
    )


async def status(
    session: AsyncSession, user_id: uuid.UUID, workspace_id: uuid.UUID
) -> TelegramLinkStatusResponse:
    link_row = await session.scalar(
        select(TelegramLink)
        .where(
            TelegramLink.user_id == user_id,
            TelegramLink.workspace_id == workspace_id,
        )
        .order_by(TelegramLink.linked_at.desc())
        .limit(1)
    )
    if link_row is None:
        return TelegramLinkStatusResponse(linked=False)
    return TelegramLinkStatusResponse(
        linked=link_row.status == "active",
        workspace_id=link_row.workspace_id,
        telegram_user_id=link_row.telegram_user_id,
        telegram_chat_id=link_row.telegram_chat_id,
        telegram_username=link_row.telegram_username,
        status=link_row.status,
        linked_at=link_row.linked_at,
        last_seen_at=link_row.last_seen_at,
    )


async def revoke_link(session: AsyncSession, context: RequestContext) -> TelegramLinkStatusResponse:
    link_row = await session.scalar(
        select(TelegramLink).where(
            TelegramLink.user_id == context.user.id,
            TelegramLink.workspace_id == context.workspace.id,
            TelegramLink.status == "active",
        )
    )
    if link_row is None:
        return TelegramLinkStatusResponse(linked=False)
    link_row.status = "revoked"
    link_row.revoked_at = datetime.now(UTC)
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="telegram_link",
        entity_id=link_row.id,
        action="telegram.unlink",
        before_data={"status": "active"},
        after_data={"status": "revoked"},
        request_id=context.request_id,
    )
    await session.commit()
    return await status(session, context.user.id, context.workspace.id)


async def handle_message(
    session: AsyncSession,
    data: TelegramMessageRequest,
    *,
    service_workspace_id: uuid.UUID | None,
    request_id: str,
) -> TelegramIntegrationResponse:
    link_row, workspace = await _active_link_context(
        session,
        data.telegram_user_id,
        data.telegram_chat_id,
        expected_workspace_id=service_workspace_id,
    )
    link_row.last_seen_at = datetime.now(UTC)
    text = " ".join(data.text.strip().split())
    command = text.split(" ", 1)[0].lower()
    if command in COMMANDS:
        return TelegramIntegrationResponse(
            status="ok", response_type="message", messages=[COMMANDS[command]]
        )
    if command == "/link":
        return TelegramIntegrationResponse(
            status="already_linked",
            response_type="message",
            messages=["Telegram уже подключён."],
        )
    if command == "/accounts":
        accounts = list(
            (
                await session.scalars(
                    select(Account)
                    .where(
                        Account.workspace_id == workspace.id,
                        Account.deleted_at.is_(None),
                        Account.is_archived.is_(False),
                    )
                    .order_by(Account.name)
                )
            ).all()
        )
        message = "Счета:\n" + "\n".join(
            f"• {account.name} ({account.currency})" for account in accounts
        )
        return TelegramIntegrationResponse(status="ok", response_type="message", messages=[message])
    if command in {"/today", "/week", "/month"}:
        return await _period_command(session, workspace, command)
    if command == "/uncategorized":
        return await _uncategorized_command(session, workspace)
    if command == "/recurring":
        return await _recurring_command(session, workspace)
    draft, ambiguity = await _parse_transaction(session, workspace, text)
    if ambiguity is not None:
        buttons: list[TelegramButton] = []
        for candidate_id, label in ambiguity.candidates:
            intent = await _new_intent(
                session,
                workspace_id=workspace.id,
                user_id=link_row.user_id,
                chat_id=link_row.telegram_chat_id,
                intent_type="selection",
                payload={
                    "draft": draft,
                    "selection_type": ambiguity.selection_type,
                    "selected_id": str(candidate_id),
                },
            )
            buttons.append(TelegramButton(label=label, callback_data=intent.opaque_id))
        return TelegramIntegrationResponse(
            status="ambiguous",
            response_type="ambiguous",
            messages=[ambiguity.message],
            buttons=buttons,
        )
    return await _preview(session, link_row, workspace, draft, request_id=request_id)


async def handle_callback(
    session: AsyncSession,
    data: TelegramCallbackRequest,
    *,
    service_workspace_id: uuid.UUID | None,
    request_id: str,
) -> TelegramIntegrationResponse:
    link_row, workspace = await _active_link_context(
        session,
        data.telegram_user_id,
        data.telegram_chat_id,
        expected_workspace_id=service_workspace_id,
    )
    intent = await session.scalar(
        select(TelegramIntent).where(TelegramIntent.opaque_id == data.opaque_id)
    )
    if intent is None:
        raise ApiError(
            status_code=404,
            code="TELEGRAM_INTENT_NOT_FOUND",
            message="Telegram intent was not found",
        )
    if intent.user_id != link_row.user_id or intent.telegram_chat_id != data.telegram_chat_id:
        raise ApiError(
            status_code=403,
            code="TELEGRAM_NOT_LINKED",
            message="Telegram callback does not belong to this user",
        )
    if intent.status == "confirmed":
        transaction_id = intent.payload.get("transaction_id")
        return TelegramIntegrationResponse(
            status="confirmed",
            response_type="confirmed",
            messages=["Операция уже подтверждена."],
            transaction_id=uuid.UUID(transaction_id) if transaction_id else None,
            duplicate=True,
        )
    if intent.status == "cancelled":
        return TelegramIntegrationResponse(
            status="cancelled",
            response_type="cancelled",
            messages=["Операция отменена."],
            duplicate=True,
        )
    now = datetime.now(UTC)
    if intent.expires_at <= now or intent.status != "pending":
        intent.status = "expired"
        intent.resolved_at = now
        raise ApiError(
            status_code=410,
            code="TELEGRAM_INTENT_EXPIRED",
            message="Telegram intent has expired",
        )
    if intent.intent_type == "selection":
        draft = dict(intent.payload["draft"])
        draft[str(intent.payload["selection_type"])] = intent.payload["selected_id"]
        intent.status = "confirmed"
        intent.resolved_at = now
        return await _preview(session, link_row, workspace, draft, request_id=request_id)
    if intent.intent_type in {"transaction_edit_account", "transaction_edit_category"}:
        draft = dict(intent.payload["draft"])
        selection_type = (
            "account_id" if intent.intent_type == "transaction_edit_account" else "category_id"
        )
        candidate_values: list[tuple[uuid.UUID, str]]
        if selection_type == "account_id":
            accounts = list(
                (
                    await session.scalars(
                        select(Account).where(
                            Account.workspace_id == workspace.id,
                            Account.currency == draft["currency"],
                            Account.deleted_at.is_(None),
                            Account.is_archived.is_(False),
                        )
                    )
                ).all()
            )
            candidate_values = [
                (item.id, item.name)
                for item in accounts
                if str(item.id) != draft.get("target_account_id")
            ]
        else:
            categories = list(
                (
                    await session.scalars(
                        select(Category).where(
                            Category.workspace_id == workspace.id,
                            Category.category_type.in_((draft["transaction_type"], "both")),
                            Category.deleted_at.is_(None),
                            Category.is_archived.is_(False),
                        )
                    )
                ).all()
            )
            candidate_values = [(item.id, item.name) for item in categories]
        buttons = []
        for candidate_id, candidate_name in candidate_values:
            selection = await _new_intent(
                session,
                workspace_id=workspace.id,
                user_id=link_row.user_id,
                chat_id=link_row.telegram_chat_id,
                intent_type="selection",
                payload={
                    "draft": draft,
                    "selection_type": selection_type,
                    "selected_id": str(candidate_id),
                },
            )
            buttons.append(TelegramButton(label=candidate_name, callback_data=selection.opaque_id))
        intent.status = "confirmed"
        intent.resolved_at = now
        return TelegramIntegrationResponse(
            status="ambiguous",
            response_type="ambiguous",
            messages=[
                "Выберите счёт." if selection_type == "account_id" else "Выберите категорию."
            ],
            buttons=buttons,
        )
    if intent.intent_type == "transaction_cancel":
        intent.status = "cancelled"
        intent.resolved_at = now
        await record_audit(
            session,
            workspace_id=workspace.id,
            actor_user_id=link_row.user_id,
            entity_type="telegram_intent",
            entity_id=intent.id,
            action="telegram.intent.cancel",
            before_data=None,
            after_data={"status": "cancelled"},
            request_id=request_id,
            source="telegram",
        )
        return TelegramIntegrationResponse(
            status="cancelled",
            response_type="cancelled",
            messages=["Операция отменена."],
        )
    if intent.intent_type != "transaction_confirm":
        raise ApiError(
            status_code=404,
            code="TELEGRAM_INTENT_NOT_FOUND",
            message="Telegram intent action is unsupported",
        )
    user = await session.get(User, link_row.user_id)
    member = await session.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.user_id == link_row.user_id,
            WorkspaceMember.workspace_id == workspace.id,
        )
    )
    if user is None or member is None:
        raise ApiError(
            status_code=403,
            code="TELEGRAM_NOT_LINKED",
            message="Linked user is unavailable",
        )
    if member.role not in {"editor", "owner"}:
        raise ApiError(
            status_code=403,
            code="ROLE_FORBIDDEN",
            message="Editor or owner role is required to create a transaction",
        )
    context = RequestContext(
        user=user,
        workspace=workspace,
        role=member.role,
        request_id=request_id,
    )
    draft = intent.payload["draft"]
    transaction = await transactions.create_transaction(
        session,
        context,
        TransactionCreate(
            occurred_at=now,
            transaction_type=draft["transaction_type"],
            amount=Decimal(draft["amount"]),
            currency=draft["currency"],
            account_id=uuid.UUID(draft["account_id"]),
            target_account_id=(
                uuid.UUID(draft["target_account_id"]) if draft.get("target_account_id") else None
            ),
            category_id=(uuid.UUID(draft["category_id"]) if draft.get("category_id") else None),
            status="confirmed",
            source="telegram",
        ),
        commit=False,
        audit_source="telegram",
    )
    intent.status = "confirmed"
    intent.resolved_at = now
    intent.payload = {**intent.payload, "transaction_id": str(transaction.id)}
    await record_audit(
        session,
        workspace_id=workspace.id,
        actor_user_id=link_row.user_id,
        entity_type="telegram_intent",
        entity_id=intent.id,
        action="telegram.intent.confirm",
        before_data=None,
        after_data={"status": "confirmed", "transaction_id": str(transaction.id)},
        request_id=request_id,
        source="telegram",
    )
    return TelegramIntegrationResponse(
        status="confirmed",
        response_type="confirmed",
        messages=["Операция подтверждена и сохранена."],
        transaction_id=transaction.id,
    )


async def _parse_transaction(
    session: AsyncSession, workspace: Workspace, text: str
) -> tuple[dict[str, str | None], Ambiguity | None]:
    match = re.match(r"^(расход|доход|перевод)\s+([^\s]+)\s+(.+)$", text, re.IGNORECASE)
    if match is None:
        raise ApiError(
            status_code=422,
            code="TELEGRAM_PARSE_AMBIGUOUS",
            message="Use: расход 1250 категория счёт",
        )
    type_word, amount_raw, remainder = match.groups()
    transaction_type = {"расход": "expense", "доход": "income", "перевод": "transfer"}[
        type_word.casefold()
    ]
    try:
        amount = Decimal(amount_raw.replace(" ", "").replace(",", "."))
    except InvalidOperation as exc:
        raise ApiError(
            status_code=422,
            code="TELEGRAM_PARSE_AMBIGUOUS",
            message="Amount is invalid",
        ) from exc
    exponent = amount.as_tuple().exponent
    if not amount.is_finite() or amount <= 0 or not isinstance(exponent, int) or exponent < -4:
        raise ApiError(
            status_code=422,
            code="TELEGRAM_PARSE_AMBIGUOUS",
            message="Amount is invalid",
        )
    normalized = _normalize_text(remainder)
    currency = workspace.base_currency
    currency_tokens = {
        "₽": "RUB",
        "\u0440\u0443\u0431": "RUB",
        "rub": "RUB",
        "usd": "USD",
        "$": "USD",
    }
    for token, code in currency_tokens.items():
        if re.search(rf"(?<!\w){re.escape(token)}(?!\w)", normalized):
            currency = code
            normalized = re.sub(rf"(?<!\w){re.escape(token)}(?!\w)", " ", normalized)
            break
    accounts = list(
        (
            await session.scalars(
                select(Account).where(
                    Account.workspace_id == workspace.id,
                    Account.currency == currency,
                    Account.deleted_at.is_(None),
                    Account.is_archived.is_(False),
                )
            )
        ).all()
    )
    account_matches = _name_matches(normalized, [(item.id, item.name) for item in accounts])
    base: dict[str, str | None] = {
        "transaction_type": transaction_type,
        "amount": str(amount),
        "currency": currency,
        "account_id": None,
        "target_account_id": None,
        "category_id": None,
    }
    if transaction_type == "transfer":
        if len(account_matches) != 2:
            return base, Ambiguity(
                selection_type="account_id",
                candidates=[(item.id, item.name) for item in accounts],
                message="Укажите два разных счёта перевода без неоднозначности.",
            )
        ordered = sorted(account_matches, key=lambda item: item[2])
        base["account_id"] = str(ordered[0][0])
        base["target_account_id"] = str(ordered[1][0])
        return base, None
    if len(account_matches) != 1:
        return base, Ambiguity(
            selection_type="account_id",
            candidates=[(item.id, item.name) for item in accounts],
            message="Выберите счёт.",
        )
    base["account_id"] = str(account_matches[0][0])
    categories = list(
        (
            await session.scalars(
                select(Category).where(
                    Category.workspace_id == workspace.id,
                    Category.category_type.in_((transaction_type, "both")),
                    Category.deleted_at.is_(None),
                    Category.is_archived.is_(False),
                )
            )
        ).all()
    )
    category_matches = _name_matches(normalized, [(item.id, item.name) for item in categories])
    if len(category_matches) != 1:
        return base, Ambiguity(
            selection_type="category_id",
            candidates=[(item.id, item.name) for item in categories],
            message="Выберите категорию.",
        )
    base["category_id"] = str(category_matches[0][0])
    return base, None


async def _preview(
    session: AsyncSession,
    link_row: TelegramLink,
    workspace: Workspace,
    draft: dict[str, str | None],
    *,
    request_id: str,
) -> TelegramIntegrationResponse:
    if not draft.get("account_id") or (
        draft["transaction_type"] != "transfer" and not draft.get("category_id")
    ):
        raise ApiError(
            status_code=422,
            code="TELEGRAM_PARSE_AMBIGUOUS",
            message="Transaction details are incomplete",
        )
    confirm = await _new_intent(
        session,
        workspace_id=workspace.id,
        user_id=link_row.user_id,
        chat_id=link_row.telegram_chat_id,
        intent_type="transaction_confirm",
        payload={"draft": draft},
    )
    cancel = await _new_intent(
        session,
        workspace_id=workspace.id,
        user_id=link_row.user_id,
        chat_id=link_row.telegram_chat_id,
        intent_type="transaction_cancel",
        payload={"draft": draft},
    )
    edit_account = await _new_intent(
        session,
        workspace_id=workspace.id,
        user_id=link_row.user_id,
        chat_id=link_row.telegram_chat_id,
        intent_type="transaction_edit_account",
        payload={"draft": draft},
    )
    edit_category = None
    if draft["transaction_type"] != "transfer":
        edit_category = await _new_intent(
            session,
            workspace_id=workspace.id,
            user_id=link_row.user_id,
            chat_id=link_row.telegram_chat_id,
            intent_type="transaction_edit_category",
            payload={"draft": draft},
        )
    await record_audit(
        session,
        workspace_id=workspace.id,
        actor_user_id=link_row.user_id,
        entity_type="telegram_intent",
        entity_id=confirm.id,
        action="telegram.intent.create",
        before_data=None,
        after_data={"intent_type": "transaction_confirm", "status": "pending"},
        request_id=request_id,
        source="telegram",
    )
    label = {
        "expense": "Расход",
        "income": "Доход",
        "transfer": "Перевод",
    }[str(draft["transaction_type"])]
    account = await session.scalar(
        select(Account).where(
            Account.id == uuid.UUID(str(draft["account_id"])),
            Account.workspace_id == workspace.id,
        )
    )
    target = (
        await session.scalar(
            select(Account).where(
                Account.id == uuid.UUID(str(draft["target_account_id"])),
                Account.workspace_id == workspace.id,
            )
        )
        if draft.get("target_account_id")
        else None
    )
    category = (
        await session.scalar(
            select(Category).where(
                Category.id == uuid.UUID(str(draft["category_id"])),
                Category.workspace_id == workspace.id,
            )
        )
        if draft.get("category_id")
        else None
    )
    details = [
        f"{label}: {draft['amount']} {draft['currency']}",
        f"Счёт: {account.name if account else '—'}",
    ]
    if target is not None:
        details.append(f"Куда: {target.name}")
    if draft["transaction_type"] != "transfer":
        details.append(f"Категория: {category.name if category else '—'}")
    details.extend(["Описание: —", "", "Подтвердить операцию?"])
    buttons = [
        TelegramButton(label="Подтвердить", callback_data=confirm.opaque_id),
        TelegramButton(label="Изменить счёт", callback_data=edit_account.opaque_id),
    ]
    if edit_category is not None:
        buttons.append(
            TelegramButton(label="Изменить категорию", callback_data=edit_category.opaque_id)
        )
    buttons.append(TelegramButton(label="Отмена", callback_data=cancel.opaque_id))
    return TelegramIntegrationResponse(
        status="preview",
        response_type="preview",
        messages=["\n".join(details)],
        buttons=buttons,
        intent_id=confirm.opaque_id,
    )


async def _new_intent(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    chat_id: int,
    intent_type: str,
    payload: dict[str, object],
) -> TelegramIntent:
    now = datetime.now(UTC)
    intent = TelegramIntent(
        opaque_id=secrets.token_urlsafe(12),
        workspace_id=workspace_id,
        user_id=user_id,
        telegram_chat_id=chat_id,
        intent_type=intent_type,
        payload=payload,
        status="pending",
        expires_at=now + INTENT_TTL,
        created_at=now,
    )
    session.add(intent)
    await session.flush()
    return intent


async def _active_link_context(
    session: AsyncSession,
    telegram_user_id: int,
    telegram_chat_id: int,
    *,
    expected_workspace_id: uuid.UUID | None = None,
) -> tuple[TelegramLink, Workspace]:
    link_row = await session.scalar(
        select(TelegramLink).where(
            TelegramLink.telegram_user_id == telegram_user_id,
            TelegramLink.telegram_chat_id == telegram_chat_id,
            TelegramLink.status == "active",
        )
    )
    if link_row is None:
        raise ApiError(
            status_code=403,
            code="TELEGRAM_NOT_LINKED",
            message="Telegram user is not linked",
        )
    if expected_workspace_id is not None and link_row.workspace_id != expected_workspace_id:
        raise ApiError(
            status_code=403,
            code="WORKSPACE_ACCESS_DENIED",
            message="Service account cannot access this Telegram workspace",
        )
    workspace = await session.scalar(
        select(Workspace)
        .join(
            WorkspaceMember,
            (WorkspaceMember.workspace_id == Workspace.id)
            & (WorkspaceMember.user_id == link_row.user_id),
        )
        .where(
            Workspace.id == link_row.workspace_id,
            Workspace.deleted_at.is_(None),
        )
    )
    if workspace is None:
        raise ApiError(
            status_code=403,
            code="TELEGRAM_NOT_LINKED",
            message="Linked workspace is unavailable",
        )
    return link_row, workspace


async def validate_delivery_target(
    session: AsyncSession,
    telegram_user_id: int,
    telegram_chat_id: int,
    *,
    service_workspace_id: uuid.UUID | None,
) -> TelegramLink:
    link_row, _ = await _active_link_context(
        session,
        telegram_user_id,
        telegram_chat_id,
        expected_workspace_id=service_workspace_id,
    )
    return link_row


async def _period_command(
    session: AsyncSession, workspace: Workspace, command: str
) -> TelegramIntegrationResponse:
    zone = ZoneInfo(workspace.timezone)
    local_now = datetime.now(UTC).astimezone(zone)
    if command == "/today":
        start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = start_local + timedelta(days=1)
        title = "Сегодня"
    elif command == "/week":
        start_local = (local_now - timedelta(days=local_now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end_local = start_local + timedelta(days=7)
        title = "Эта неделя"
    else:
        start_local = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end_local = (
            start_local.replace(year=start_local.year + 1, month=1)
            if start_local.month == 12
            else start_local.replace(month=start_local.month + 1)
        )
        title = "Этот месяц"
    items = list(
        (
            await session.scalars(
                select(FinancialTransaction).where(
                    FinancialTransaction.workspace_id == workspace.id,
                    FinancialTransaction.occurred_at >= start_local.astimezone(UTC),
                    FinancialTransaction.occurred_at < end_local.astimezone(UTC),
                    FinancialTransaction.status.in_(("confirmed", "reconciled")),
                    FinancialTransaction.deleted_at.is_(None),
                )
            )
        ).all()
    )
    totals: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {"income": Decimal("0"), "expense": Decimal("0")}
    )
    for item in items:
        if item.transaction_type in {"income", "expense"}:
            totals[item.currency][item.transaction_type] += item.amount
    lines = [f"{title}: {len(items)} операций"]
    for currency, values in sorted(totals.items()):
        lines.append(
            f"{currency}: доход {values['income']:.2f}, расход {values['expense']:.2f}, "
            f"поток {values['income'] - values['expense']:+.2f}"
        )
    if not totals:
        lines.append("Подтверждённых доходов и расходов нет.")
    return TelegramIntegrationResponse(
        status="ok", response_type="message", messages=["\n".join(lines)]
    )


async def _uncategorized_command(
    session: AsyncSession, workspace: Workspace
) -> TelegramIntegrationResponse:
    items = list(
        (
            await session.scalars(
                select(FinancialTransaction).where(
                    FinancialTransaction.workspace_id == workspace.id,
                    FinancialTransaction.transaction_type.in_(("income", "expense")),
                    FinancialTransaction.category_id.is_(None),
                    FinancialTransaction.status.in_(("confirmed", "reconciled")),
                    FinancialTransaction.deleted_at.is_(None),
                )
            )
        ).all()
    )
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for item in items:
        totals[item.currency] += item.amount
    lines = [f"Без категории: {len(items)}"]
    lines.extend(f"{currency}: {amount:.2f}" for currency, amount in sorted(totals.items()))
    return TelegramIntegrationResponse(
        status="ok", response_type="message", messages=["\n".join(lines)]
    )


async def _recurring_command(
    session: AsyncSession, workspace: Workspace
) -> TelegramIntegrationResponse:
    items = list(
        (
            await session.scalars(
                select(RecurringRule)
                .where(
                    RecurringRule.workspace_id == workspace.id,
                    RecurringRule.is_active.is_(True),
                    RecurringRule.deleted_at.is_(None),
                )
                .order_by(RecurringRule.next_run_at.asc().nullslast())
                .limit(10)
            )
        ).all()
    )
    lines = ["Ближайшие регулярные операции:"]
    lines.extend(
        f"• {item.name} — {item.amount:.2f} {item.currency}, "
        f"{item.next_run_at.isoformat() if item.next_run_at else 'без даты'}"
        for item in items
    )
    if not items:
        lines.append("Активных правил нет.")
    return TelegramIntegrationResponse(
        status="ok", response_type="message", messages=["\n".join(lines)]
    )


def _name_matches(
    text: str, candidates: list[tuple[uuid.UUID, str]]
) -> list[tuple[uuid.UUID, str, int]]:
    matches: list[tuple[uuid.UUID, str, int]] = []
    for candidate_id, name in candidates:
        normalized = _normalize_text(name)
        match = re.search(rf"(?<!\w){re.escape(normalized)}(?!\w)", text)
        if match is not None:
            matches.append((candidate_id, name, match.start()))
    return matches


def _normalize_code(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _normalize_text(value: str) -> str:
    return " ".join(value.casefold().replace("\u0451", "\u0435").split())


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
