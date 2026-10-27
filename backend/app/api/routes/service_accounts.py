import uuid

from fastapi import APIRouter, Query

from app.dependencies.context import OwnerContext
from app.dependencies.database import DbSession
from app.schemas.automations import (
    ServiceAccountActionResponse,
    ServiceAccountCreate,
    ServiceAccountPage,
    ServiceKeyCreate,
    ServiceKeyOneTimeResponse,
)
from app.schemas.common import PageMeta
from app.services import service_accounts as service

router = APIRouter()


@router.get("", response_model=ServiceAccountPage)
async def service_account_list(
    context: OwnerContext,
    session: DbSession,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ServiceAccountPage:
    items, total = await service.list_accounts(
        session, context.workspace.id, limit=limit, offset=offset
    )
    return ServiceAccountPage(
        items=[await service.response(session, item) for item in items],
        page=PageMeta(limit=limit, offset=offset, total=total),
    )


@router.post("", response_model=ServiceAccountActionResponse, status_code=201)
async def service_account_create(
    data: ServiceAccountCreate,
    context: OwnerContext,
    session: DbSession,
) -> ServiceAccountActionResponse:
    account = await service.create_account(session, context, data)
    return ServiceAccountActionResponse(
        status="created", service_account=await service.response(session, account)
    )


@router.post("/{account_id}/keys", response_model=ServiceKeyOneTimeResponse, status_code=201)
async def service_account_key_create(
    account_id: uuid.UUID,
    data: ServiceKeyCreate,
    context: OwnerContext,
    session: DbSession,
) -> ServiceKeyOneTimeResponse:
    return await service.issue_key(session, context, account_id, data, revoke_existing=False)


@router.post("/{account_id}/rotate-key", response_model=ServiceKeyOneTimeResponse)
async def service_account_key_rotate(
    account_id: uuid.UUID,
    data: ServiceKeyCreate,
    context: OwnerContext,
    session: DbSession,
) -> ServiceKeyOneTimeResponse:
    return await service.issue_key(session, context, account_id, data, revoke_existing=True)


@router.post("/{account_id}/revoke", response_model=ServiceAccountActionResponse)
async def service_account_revoke(
    account_id: uuid.UUID,
    context: OwnerContext,
    session: DbSession,
) -> ServiceAccountActionResponse:
    account = await service.revoke_account(session, context, account_id)
    return ServiceAccountActionResponse(
        status="revoked", service_account=await service.response(session, account)
    )


@router.delete("/{account_id}", response_model=ServiceAccountActionResponse)
async def service_account_delete(
    account_id: uuid.UUID,
    context: OwnerContext,
    session: DbSession,
) -> ServiceAccountActionResponse:
    account = await service.revoke_account(session, context, account_id)
    return ServiceAccountActionResponse(
        status="revoked", service_account=await service.response(session, account)
    )
