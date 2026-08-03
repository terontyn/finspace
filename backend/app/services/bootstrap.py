from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ApiError
from app.db.models.accounts import Account
from app.db.models.categories import Category
from app.db.models.users import User, Workspace, WorkspaceMember
from app.schemas.users import BootstrapResponse
from app.services.audit import record_audit, snapshot

BOOTSTRAP_EMAIL = "developer@finspace.local"


async def bootstrap_development(session: AsyncSession, *, request_id: str) -> BootstrapResponse:
    if settings.environment != "development":
        raise ApiError(status_code=404, code="HTTP_ERROR", message="Resource was not found")

    created = False
    user = await session.scalar(select(User).where(User.normalized_email == BOOTSTRAP_EMAIL))
    if user is None:
        user = User(
            email=BOOTSTRAP_EMAIL,
            normalized_email=BOOTSTRAP_EMAIL,
            display_name="Локальный пользователь",
            locale="ru-RU",
            timezone="Europe/Amsterdam",
        )
        session.add(user)
        await session.flush()
        created = True

    workspace = await session.scalar(
        select(Workspace).where(
            Workspace.owner_user_id == user.id,
            Workspace.name == "Личное пространство",
            Workspace.deleted_at.is_(None),
        )
    )
    if workspace is None:
        workspace = Workspace(
            name="Личное пространство",
            base_currency="RUB",
            timezone=user.timezone,
            owner_user_id=user.id,
        )
        session.add(workspace)
        await session.flush()
        created = True

    member = await session.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.user_id == user.id,
        )
    )
    if member is None:
        session.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="owner"))
        created = True

    now = datetime.now(UTC)
    account_seeds = [
        ("Наличные", "cash"),
        ("Основная карта", "debit_card"),
        ("Накопительный счёт", "savings"),
    ]
    for name, account_type in account_seeds:
        account = await session.scalar(
            select(Account).where(
                Account.workspace_id == workspace.id,
                Account.name == name,
                Account.deleted_at.is_(None),
            )
        )
        if account is None:
            account = Account(
                workspace_id=workspace.id,
                name=name,
                account_type=account_type,
                currency="RUB",
                opening_balance=Decimal("0"),
                opening_balance_at=now,
            )
            session.add(account)
            await session.flush()
            await record_audit(
                session,
                workspace_id=workspace.id,
                actor_user_id=user.id,
                entity_type="account",
                entity_id=account.id,
                action="create",
                before_data=None,
                after_data=snapshot("account", account),
                request_id=request_id,
                source="system",
            )
            created = True

    category_seeds = {
        "income": ["Зарплата", "Дополнительный доход", "Возвраты"],
        "expense": [
            "Продукты",
            "Транспорт",
            "Жильё",
            "Здоровье",
            "Развлечения",
            "Подписки",
            "Прочее",
        ],
    }
    for category_type, names in category_seeds.items():
        for sort_order, name in enumerate(names):
            category = await session.scalar(
                select(Category).where(
                    Category.workspace_id == workspace.id,
                    Category.parent_id.is_(None),
                    Category.name == name,
                    Category.deleted_at.is_(None),
                )
            )
            if category is None:
                category = Category(
                    workspace_id=workspace.id,
                    name=name,
                    category_type=category_type,
                    sort_order=sort_order,
                )
                session.add(category)
                await session.flush()
                await record_audit(
                    session,
                    workspace_id=workspace.id,
                    actor_user_id=user.id,
                    entity_type="category",
                    entity_id=category.id,
                    action="create",
                    before_data=None,
                    after_data=snapshot("category", category),
                    request_id=request_id,
                    source="system",
                )
                created = True

    await session.commit()
    return BootstrapResponse(user_id=user.id, workspace_id=workspace.id, created=created)
