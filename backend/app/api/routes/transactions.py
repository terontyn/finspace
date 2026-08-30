import uuid
from datetime import datetime

from fastapi import APIRouter, Query

from app.core.errors import ApiError
from app.dependencies.context import CurrentContext, EditorContext
from app.dependencies.database import DbSession
from app.repositories import transactions as repository
from app.schemas.accounts import VersionRequest
from app.schemas.audit import AuditPage, AuditResponse
from app.schemas.categorization_rules import (
    CategorizationApplyRequest,
    CategorizationApplyResponse,
    CategorizationRuleResponse,
)
from app.schemas.common import PageMeta
from app.schemas.transactions import (
    EntityRef,
    TransactionCreate,
    TransactionPage,
    TransactionResponse,
    TransactionStatus,
    TransactionType,
    TransactionUpdate,
)
from app.services import categorization_rules as categorization_service
from app.services import transactions as service
from app.services.audit import list_audit_entries

router = APIRouter()


@router.get("", response_model=TransactionPage)
async def transaction_list(
    context: CurrentContext,
    session: DbSession,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    transaction_type: TransactionType | None = None,
    status: TransactionStatus | None = None,
    account_id: uuid.UUID | None = None,
    category_id: uuid.UUID | None = None,
    payee_id: uuid.UUID | None = None,
    search: str | None = Query(default=None, max_length=300),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> TransactionPage:
    items, total = await repository.list_transactions(
        session,
        context.workspace.id,
        date_from=date_from,
        date_to=date_to,
        transaction_type=transaction_type,
        status=status,
        account_id=account_id,
        category_id=category_id,
        payee_id=payee_id,
        search=search,
        limit=limit,
        offset=offset,
    )
    return TransactionPage(
        items=await service.transaction_page_responses(session, context.workspace.id, items),
        page=PageMeta(limit=limit, offset=offset, total=total),
    )


@router.post("", response_model=TransactionResponse, status_code=201)
async def transaction_create(
    data: TransactionCreate,
    context: EditorContext,
    session: DbSession,
) -> TransactionResponse:
    transaction = await service.create_transaction(session, context, data)
    return await service.transaction_response(session, transaction)


@router.get("/{transaction_id}", response_model=TransactionResponse)
async def transaction_get(
    transaction_id: uuid.UUID,
    context: CurrentContext,
    session: DbSession,
) -> TransactionResponse:
    transaction = await repository.get_transaction(session, context.workspace.id, transaction_id)
    if transaction is None:
        raise ApiError(
            status_code=404,
            code="TRANSACTION_NOT_FOUND",
            message="Transaction was not found",
        )
    return await service.transaction_response(session, transaction)


@router.patch("/{transaction_id}", response_model=TransactionResponse)
async def transaction_update(
    transaction_id: uuid.UUID,
    data: TransactionUpdate,
    context: EditorContext,
    session: DbSession,
) -> TransactionResponse:
    transaction = await service.update_transaction(session, context, transaction_id, data)
    return await service.transaction_response(session, transaction)


@router.delete("/{transaction_id}", response_model=TransactionResponse)
async def transaction_delete(
    transaction_id: uuid.UUID,
    context: EditorContext,
    session: DbSession,
    version: int = Query(ge=1),
) -> TransactionResponse:
    transaction = await service.delete_transaction(session, context, transaction_id, version)
    return await service.transaction_response(session, transaction)


@router.post("/{transaction_id}/restore", response_model=TransactionResponse)
async def transaction_restore(
    transaction_id: uuid.UUID,
    data: VersionRequest,
    context: EditorContext,
    session: DbSession,
) -> TransactionResponse:
    transaction = await service.restore_transaction(session, context, transaction_id, data.version)
    return await service.transaction_response(session, transaction)


@router.post("/{transaction_id}/cancel", response_model=TransactionResponse)
async def transaction_cancel(
    transaction_id: uuid.UUID,
    data: VersionRequest,
    context: EditorContext,
    session: DbSession,
) -> TransactionResponse:
    transaction = await service.cancel_transaction(session, context, transaction_id, data.version)
    return await service.transaction_response(session, transaction)


@router.post("/{transaction_id}/confirm", response_model=TransactionResponse)
async def transaction_confirm(
    transaction_id: uuid.UUID,
    data: VersionRequest,
    context: EditorContext,
    session: DbSession,
) -> TransactionResponse:
    transaction = await service.confirm_transaction(session, context, transaction_id, data.version)
    return await service.transaction_response(session, transaction)


@router.post("/{transaction_id}/apply-categorization", response_model=CategorizationApplyResponse)
async def transaction_apply_categorization(
    transaction_id: uuid.UUID,
    data: CategorizationApplyRequest,
    context: EditorContext,
    session: DbSession,
) -> CategorizationApplyResponse:
    result = await categorization_service.apply_to_transaction(
        session,
        context,
        transaction_id,
        data.version,
    )
    match = result.match
    return CategorizationApplyResponse(
        applied=result.applied,
        reason=result.reason,
        rule=(
            CategorizationRuleResponse.model_validate(match.rule) if match is not None else None
        ),
        category=(
            EntityRef(id=match.category.id, name=match.category.name) if match is not None else None
        ),
        transaction=await service.transaction_response(session, result.transaction),
    )


@router.get("/{transaction_id}/history", response_model=AuditPage)
async def transaction_history(
    transaction_id: uuid.UUID,
    context: EditorContext,
    session: DbSession,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AuditPage:
    items, total = await list_audit_entries(
        session,
        context.workspace.id,
        entity_type="transaction",
        entity_id=transaction_id,
        date_from=None,
        date_to=None,
        limit=limit,
        offset=offset,
    )
    return AuditPage(
        items=[AuditResponse.model_validate(item) for item in items],
        page=PageMeta(limit=limit, offset=offset, total=total),
    )
