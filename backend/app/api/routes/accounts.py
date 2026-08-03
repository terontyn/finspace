import uuid

from fastapi import APIRouter, Query

from app.core.errors import ApiError
from app.dependencies.context import CurrentContext, EditorContext
from app.dependencies.database import DbSession
from app.repositories import accounts as repository
from app.schemas.accounts import (
    AccountBalance,
    AccountCreate,
    AccountPage,
    AccountResponse,
    AccountUpdate,
    VersionRequest,
)
from app.schemas.common import PageMeta
from app.services import accounts as service
from app.services.calculations import calculate_balances

router = APIRouter()


@router.get("/balances", response_model=list[AccountBalance])
async def balances(context: CurrentContext, session: DbSession) -> list[AccountBalance]:
    return await calculate_balances(session, context.workspace.id)


@router.get("", response_model=AccountPage)
async def account_list(
    context: CurrentContext,
    session: DbSession,
    is_archived: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AccountPage:
    items, total = await repository.list_accounts(
        session,
        context.workspace.id,
        is_archived=is_archived,
        limit=limit,
        offset=offset,
    )
    return AccountPage(
        items=[AccountResponse.model_validate(item) for item in items],
        page=PageMeta(limit=limit, offset=offset, total=total),
    )


@router.post("", response_model=AccountResponse, status_code=201)
async def account_create(
    data: AccountCreate,
    context: EditorContext,
    session: DbSession,
) -> AccountResponse:
    return AccountResponse.model_validate(await service.create_account(session, context, data))


@router.get("/{account_id}", response_model=AccountResponse)
async def account_get(
    account_id: uuid.UUID,
    context: CurrentContext,
    session: DbSession,
) -> AccountResponse:
    account = await repository.get_account(session, context.workspace.id, account_id)
    if account is None:
        raise ApiError(status_code=404, code="ACCOUNT_NOT_FOUND", message="Account was not found")
    return AccountResponse.model_validate(account)


@router.patch("/{account_id}", response_model=AccountResponse)
async def account_update(
    account_id: uuid.UUID,
    data: AccountUpdate,
    context: EditorContext,
    session: DbSession,
) -> AccountResponse:
    return AccountResponse.model_validate(
        await service.update_account(session, context, account_id, data)
    )


@router.delete("/{account_id}", response_model=AccountResponse)
async def account_delete(
    account_id: uuid.UUID,
    context: EditorContext,
    session: DbSession,
    version: int = Query(ge=1),
) -> AccountResponse:
    return AccountResponse.model_validate(
        await service.delete_account(session, context, account_id, version)
    )


@router.post("/{account_id}/restore", response_model=AccountResponse)
async def account_restore(
    account_id: uuid.UUID,
    data: VersionRequest,
    context: EditorContext,
    session: DbSession,
) -> AccountResponse:
    return AccountResponse.model_validate(
        await service.restore_account(session, context, account_id, data.version)
    )
