import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models.accounts import Account
from app.dependencies.context import RequestContext
from app.repositories import accounts as repository
from app.schemas.accounts import AccountCreate, AccountUpdate
from app.services.audit import record_audit, snapshot
from app.services.sync_outbox import enqueue_entity


def _validate_credit_limit(account_type: str, credit_limit: object) -> None:
    if credit_limit is not None and account_type != "credit_card":
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="credit_limit is allowed only for credit_card accounts",
        )


async def create_account(
    session: AsyncSession, context: RequestContext, data: AccountCreate
) -> Account:
    if await repository.find_active_name(session, context.workspace.id, data.name):
        raise ApiError(
            status_code=409, code="DUPLICATE_NAME", message="Account name already exists"
        )
    _validate_credit_limit(data.account_type, data.credit_limit)
    account = Account(workspace_id=context.workspace.id, **data.model_dump())
    session.add(account)
    await session.flush()
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="account",
        entity_id=account.id,
        action="create",
        before_data=None,
        after_data=snapshot("account", account),
        request_id=context.request_id,
    )
    await enqueue_entity(
        session,
        workspace_id=context.workspace.id,
        entity_type="account",
        entity=account,
    )
    await session.commit()
    return account


async def update_account(
    session: AsyncSession,
    context: RequestContext,
    account_id: uuid.UUID,
    data: AccountUpdate,
    *,
    commit: bool = True,
    audit_source: str = "api",
) -> Account:
    account = await repository.get_account(session, context.workspace.id, account_id)
    if account is None:
        raise ApiError(status_code=404, code="ACCOUNT_NOT_FOUND", message="Account was not found")
    if account.version != data.version:
        raise ApiError(
            status_code=409,
            code="VERSION_CONFLICT",
            message="Account was modified by another request",
            details={"current_version": account.version},
        )
    changes = data.model_dump(exclude_unset=True, exclude={"version"})
    next_name = str(changes.get("name", account.name))
    next_archived = bool(changes.get("is_archived", account.is_archived))
    if not next_archived and await repository.find_active_name(
        session, context.workspace.id, next_name, exclude_id=account.id
    ):
        raise ApiError(
            status_code=409, code="DUPLICATE_NAME", message="Account name already exists"
        )
    _validate_credit_limit(
        str(changes.get("account_type", account.account_type)),
        changes.get("credit_limit", account.credit_limit),
    )
    before = snapshot("account", account)
    was_archived = account.is_archived
    for field, value in changes.items():
        setattr(account, field, value)
    account.version += 1
    account.updated_at = datetime.now(UTC)
    await session.flush()
    action = "archive" if not was_archived and account.is_archived else "update"
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="account",
        entity_id=account.id,
        action=action,
        before_data=before,
        after_data=snapshot("account", account),
        request_id=context.request_id,
        source=audit_source,
    )
    await enqueue_entity(
        session,
        workspace_id=context.workspace.id,
        entity_type="account",
        entity=account,
        operation="archive" if action == "archive" else "upsert",
    )
    if commit:
        await session.commit()
    return account


async def delete_account(
    session: AsyncSession, context: RequestContext, account_id: uuid.UUID, version: int
) -> Account:
    account = await repository.get_account(session, context.workspace.id, account_id)
    if account is None:
        raise ApiError(status_code=404, code="ACCOUNT_NOT_FOUND", message="Account was not found")
    if account.version != version:
        raise ApiError(status_code=409, code="VERSION_CONFLICT", message="Version is stale")
    before = snapshot("account", account)
    account.deleted_at = datetime.now(UTC)
    account.version += 1
    await session.flush()
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="account",
        entity_id=account.id,
        action="delete",
        before_data=before,
        after_data=snapshot("account", account),
        request_id=context.request_id,
    )
    await enqueue_entity(
        session,
        workspace_id=context.workspace.id,
        entity_type="account",
        entity=account,
        operation="delete",
    )
    await session.commit()
    return account


async def restore_account(
    session: AsyncSession, context: RequestContext, account_id: uuid.UUID, version: int
) -> Account:
    account = await repository.get_account(
        session, context.workspace.id, account_id, include_deleted=True
    )
    if account is None:
        raise ApiError(status_code=404, code="ACCOUNT_NOT_FOUND", message="Account was not found")
    if account.version != version:
        raise ApiError(status_code=409, code="VERSION_CONFLICT", message="Version is stale")
    if account.deleted_at is None:
        return account
    if not account.is_archived and await repository.find_active_name(
        session, context.workspace.id, account.name, exclude_id=account.id
    ):
        raise ApiError(status_code=409, code="DUPLICATE_NAME", message="Name is in use")
    before = snapshot("account", account)
    account.deleted_at = None
    account.version += 1
    await session.flush()
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="account",
        entity_id=account.id,
        action="restore",
        before_data=before,
        after_data=snapshot("account", account),
        request_id=context.request_id,
    )
    await enqueue_entity(
        session,
        workspace_id=context.workspace.id,
        entity_type="account",
        entity=account,
        operation="restore",
    )
    await session.commit()
    return account
