import hashlib
import secrets
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models.automations import ServiceAccount, ServiceApiKey
from app.dependencies.context import RequestContext
from app.schemas.automations import (
    SAFE_SERVICE_PERMISSIONS,
    ServiceAccountCreate,
    ServiceAccountResponse,
    ServiceApiKeyResponse,
    ServiceKeyCreate,
    ServiceKeyOneTimeResponse,
)
from app.services.audit import record_audit


async def _keys(session: AsyncSession, account_id: uuid.UUID) -> list[ServiceApiKey]:
    return list(
        (
            await session.scalars(
                select(ServiceApiKey)
                .where(ServiceApiKey.service_account_id == account_id)
                .order_by(ServiceApiKey.created_at.desc(), ServiceApiKey.id.desc())
            )
        ).all()
    )


async def response(session: AsyncSession, account: ServiceAccount) -> ServiceAccountResponse:
    keys = await _keys(session, account.id)
    return ServiceAccountResponse(
        id=account.id,
        workspace_id=account.workspace_id,
        name=account.name,
        service_type=account.service_type,  # type: ignore[arg-type]
        status=account.status,  # type: ignore[arg-type]
        permissions=list(account.permissions),
        created_by=account.created_by,
        created_at=account.created_at,
        updated_at=account.updated_at,
        revoked_at=account.revoked_at,
        last_used_at=account.last_used_at,
        keys=[ServiceApiKeyResponse.model_validate(item) for item in keys],
    )


async def list_accounts(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> tuple[list[ServiceAccount], int]:
    filters = [ServiceAccount.workspace_id == workspace_id]
    total = int(
        await session.scalar(select(func.count()).select_from(ServiceAccount).where(*filters)) or 0
    )
    items = list(
        (
            await session.scalars(
                select(ServiceAccount)
                .where(*filters)
                .order_by(ServiceAccount.created_at.desc(), ServiceAccount.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return items, total


async def get_account(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    account_id: uuid.UUID,
) -> ServiceAccount:
    account = await session.scalar(
        select(ServiceAccount).where(
            ServiceAccount.id == account_id,
            ServiceAccount.workspace_id == workspace_id,
        )
    )
    if account is None:
        raise ApiError(
            status_code=404,
            code="SERVICE_ACCOUNT_NOT_FOUND",
            message="Service account was not found",
        )
    return account


async def create_account(
    session: AsyncSession,
    context: RequestContext,
    data: ServiceAccountCreate,
) -> ServiceAccount:
    default_permissions = (
        sorted(SAFE_SERVICE_PERMISSIONS)
        if data.service_type == "n8n"
        else ["backup:status"]
        if data.service_type == "backup_agent"
        else ["automation:read"]
    )
    account = ServiceAccount(
        workspace_id=context.workspace.id,
        name=data.name.strip(),
        service_type=data.service_type,
        status="active",
        permissions=data.permissions or default_permissions,
        created_by=context.user.id,
    )
    session.add(account)
    await session.flush()
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="service_account",
        entity_id=account.id,
        action="service_account.create",
        before_data=None,
        after_data={
            "name": account.name,
            "service_type": account.service_type,
            "permissions": account.permissions,
        },
        request_id=context.request_id,
    )
    await session.commit()
    await session.refresh(account)
    return account


async def issue_key(
    session: AsyncSession,
    context: RequestContext,
    account_id: uuid.UUID,
    data: ServiceKeyCreate,
    *,
    revoke_existing: bool,
) -> ServiceKeyOneTimeResponse:
    account = await get_account(session, context.workspace.id, account_id)
    if account.status != "active":
        raise ApiError(
            status_code=409,
            code="SERVICE_ACCOUNT_REVOKED",
            message="Cannot issue a key for an inactive service account",
        )
    now = datetime.now(UTC)
    if data.expires_at is not None and data.expires_at <= now:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="Service key expiration must be in the future",
        )
    if revoke_existing:
        for existing in await _keys(session, account.id):
            if existing.revoked_at is None:
                existing.revoked_at = now
    prefix = f"fsk_{secrets.token_hex(6)}"
    secret = f"{prefix}.{secrets.token_urlsafe(48)}"
    key = ServiceApiKey(
        service_account_id=account.id,
        key_prefix=prefix,
        key_hash=hashlib.sha256(secret.encode()).hexdigest(),
        created_at=now,
        expires_at=data.expires_at,
    )
    session.add(key)
    await session.flush()
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="service_account",
        entity_id=account.id,
        action="service_account.key.rotate",
        before_data=None,
        after_data={"key_prefix": prefix, "revoke_existing": revoke_existing},
        request_id=context.request_id,
    )
    await session.commit()
    await session.refresh(account)
    return ServiceKeyOneTimeResponse(
        service_account=await response(session, account),
        key=secret,
        warning="Секрет показан один раз. Сохраните ключ только в credentials n8n.",
    )


async def revoke_account(
    session: AsyncSession,
    context: RequestContext,
    account_id: uuid.UUID,
) -> ServiceAccount:
    account = await get_account(session, context.workspace.id, account_id)
    if account.status == "revoked":
        return account
    now = datetime.now(UTC)
    account.status = "revoked"
    account.revoked_at = now
    for key in await _keys(session, account.id):
        if key.revoked_at is None:
            key.revoked_at = now
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="service_account",
        entity_id=account.id,
        action="service_account.revoke",
        before_data=None,
        after_data={"status": "revoked"},
        request_id=context.request_id,
    )
    await session.commit()
    await session.refresh(account)
    return account
