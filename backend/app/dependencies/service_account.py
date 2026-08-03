import hashlib
import hmac
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, Header, Request
from sqlalchemy import select

from app.core.errors import ApiError
from app.db.models.automations import ServiceAccount, ServiceApiKey
from app.dependencies.database import DbSession


@dataclass(frozen=True, slots=True)
class ServiceAccountContext:
    service_account: ServiceAccount
    workspace_id: uuid.UUID | None
    permissions: frozenset[str]
    request_id: str


async def get_service_account_context(
    request: Request,
    session: DbSession,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> ServiceAccountContext:
    if authorization is None or not authorization.lower().startswith("servicekey "):
        raise ApiError(
            status_code=401,
            code="SERVICE_KEY_INVALID",
            message="A service API key is required",
        )
    raw_key = authorization.split(" ", 1)[1].strip()
    prefix, separator, _ = raw_key.partition(".")
    if not separator or not prefix.startswith("fsk_") or len(raw_key) > 200:
        raise ApiError(
            status_code=401,
            code="SERVICE_KEY_INVALID",
            message="Service API key is invalid",
        )
    key = await session.scalar(select(ServiceApiKey).where(ServiceApiKey.key_prefix == prefix))
    now = datetime.now(UTC)
    if (
        key is None
        or key.revoked_at is not None
        or (key.expires_at is not None and key.expires_at <= now)
        or not hmac.compare_digest(hashlib.sha256(raw_key.encode()).hexdigest(), key.key_hash)
    ):
        raise ApiError(
            status_code=401,
            code="SERVICE_KEY_INVALID",
            message="Service API key is invalid, expired or revoked",
        )
    account = await session.get(ServiceAccount, key.service_account_id)
    if account is None:
        raise ApiError(
            status_code=401,
            code="SERVICE_ACCOUNT_NOT_FOUND",
            message="Service account was not found",
        )
    if account.status != "active" or account.revoked_at is not None:
        raise ApiError(
            status_code=403,
            code="SERVICE_ACCOUNT_REVOKED",
            message="Service account is not active",
        )
    key.last_used_at = now
    account.last_used_at = now
    await session.commit()
    return ServiceAccountContext(
        service_account=account,
        workspace_id=account.workspace_id,
        permissions=frozenset(account.permissions),
        request_id=str(getattr(request.state, "request_id", "")),
    )


def require_service_permission(
    permission: str,
) -> Callable[..., Coroutine[Any, Any, ServiceAccountContext]]:
    async def dependency(
        context: Annotated[ServiceAccountContext, Depends(get_service_account_context)],
    ) -> ServiceAccountContext:
        if permission not in context.permissions:
            raise ApiError(
                status_code=403,
                code="SERVICE_PERMISSION_DENIED",
                message=f"Service permission {permission} is required",
            )
        return context

    return dependency


def ensure_service_workspace(context: ServiceAccountContext, workspace_id: uuid.UUID) -> None:
    if context.workspace_id is not None and context.workspace_id != workspace_id:
        raise ApiError(
            status_code=403,
            code="WORKSPACE_ACCESS_DENIED",
            message="Service account is not scoped to this workspace",
        )
