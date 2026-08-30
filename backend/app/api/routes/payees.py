import uuid

from fastapi import APIRouter, Query

from app.core.errors import ApiError
from app.dependencies.context import CurrentContext, EditorContext
from app.dependencies.database import DbSession
from app.repositories import payees as repository
from app.schemas.accounts import VersionRequest
from app.schemas.common import PageMeta
from app.schemas.payees import (
    PayeeAliasCreate,
    PayeeCreate,
    PayeePage,
    PayeeResponse,
    PayeeUpdate,
)
from app.services import payees as service

router = APIRouter()


@router.get("", response_model=PayeePage)
async def payee_list(
    context: CurrentContext,
    session: DbSession,
    search: str | None = Query(default=None, max_length=300),
    include_deleted: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> PayeePage:
    items, total = await repository.list_payees(
        session,
        context.workspace.id,
        search=search,
        include_deleted=include_deleted,
        limit=limit,
        offset=offset,
    )
    return PayeePage(
        items=[service.payee_response(item) for item in items],
        page=PageMeta(limit=limit, offset=offset, total=total),
    )


@router.post("", response_model=PayeeResponse, status_code=201)
async def payee_create(
    data: PayeeCreate,
    context: EditorContext,
    session: DbSession,
) -> PayeeResponse:
    return service.payee_response(await service.create_payee(session, context, data))


@router.get("/{payee_id}", response_model=PayeeResponse)
async def payee_get(
    payee_id: uuid.UUID,
    context: CurrentContext,
    session: DbSession,
    include_deleted: bool = False,
) -> PayeeResponse:
    payee = await repository.get_payee(
        session,
        context.workspace.id,
        payee_id,
        include_deleted=include_deleted,
        include_aliases=True,
    )
    if payee is None:
        raise ApiError(status_code=404, code="PAYEE_NOT_FOUND", message="Payee was not found")
    return service.payee_response(payee)


@router.patch("/{payee_id}", response_model=PayeeResponse)
async def payee_update(
    payee_id: uuid.UUID,
    data: PayeeUpdate,
    context: EditorContext,
    session: DbSession,
) -> PayeeResponse:
    return service.payee_response(await service.update_payee(session, context, payee_id, data))


@router.delete("/{payee_id}", response_model=PayeeResponse)
async def payee_delete(
    payee_id: uuid.UUID,
    context: EditorContext,
    session: DbSession,
    version: int = Query(ge=1),
) -> PayeeResponse:
    return service.payee_response(await service.delete_payee(session, context, payee_id, version))


@router.post("/{payee_id}/restore", response_model=PayeeResponse)
async def payee_restore(
    payee_id: uuid.UUID,
    data: VersionRequest,
    context: EditorContext,
    session: DbSession,
) -> PayeeResponse:
    return service.payee_response(
        await service.restore_payee(session, context, payee_id, data.version)
    )


@router.post("/{payee_id}/aliases", response_model=PayeeResponse, status_code=201)
async def payee_alias_create(
    payee_id: uuid.UUID,
    data: PayeeAliasCreate,
    context: EditorContext,
    session: DbSession,
) -> PayeeResponse:
    return service.payee_response(await service.create_alias(session, context, payee_id, data))


@router.delete("/{payee_id}/aliases/{alias_id}", response_model=PayeeResponse)
async def payee_alias_delete(
    payee_id: uuid.UUID,
    alias_id: uuid.UUID,
    context: EditorContext,
    session: DbSession,
    version: int = Query(ge=1),
) -> PayeeResponse:
    return service.payee_response(
        await service.delete_alias(session, context, payee_id, alias_id, version)
    )


@router.post("/{payee_id}/aliases/{alias_id}/restore", response_model=PayeeResponse)
async def payee_alias_restore(
    payee_id: uuid.UUID,
    alias_id: uuid.UUID,
    data: VersionRequest,
    context: EditorContext,
    session: DbSession,
) -> PayeeResponse:
    return service.payee_response(
        await service.restore_alias(
            session,
            context,
            payee_id,
            alias_id,
            data.version,
        )
    )
