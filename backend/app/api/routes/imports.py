import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, Header, Query, UploadFile
from sqlalchemy import func, select

from app.db.models.imports import ImportBatch, ImportRow
from app.dependencies.context import EditorContext
from app.dependencies.database import DbSession
from app.schemas.common import PageMeta
from app.schemas.imports import (
    ImportActionResponse,
    ImportBatchPage,
    ImportBatchResponse,
    ImportCommitRequest,
    ImportMappingRequest,
    ImportRollbackRequest,
    ImportRowOverrideRequest,
    ImportRowPage,
    ImportRowResponse,
)
from app.services import imports as service

router = APIRouter()


@router.post("", response_model=ImportBatchResponse, status_code=201)
async def import_upload(
    context: EditorContext,
    session: DbSession,
    file: Annotated[UploadFile, File()],
    force_duplicate: Annotated[bool, Form()] = False,
) -> ImportBatchResponse:
    batch = await service.upload_import(session, context, file, force_duplicate=force_duplicate)
    return ImportBatchResponse.model_validate(batch)


@router.get("", response_model=ImportBatchPage)
async def import_list(
    context: EditorContext,
    session: DbSession,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ImportBatchPage:
    filters = [ImportBatch.workspace_id == context.workspace.id]
    if status is not None:
        filters.append(ImportBatch.status == status)
    total = int(
        await session.scalar(select(func.count()).select_from(ImportBatch).where(*filters)) or 0
    )
    items = list(
        (
            await session.scalars(
                select(ImportBatch)
                .where(*filters)
                .order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return ImportBatchPage(
        items=[ImportBatchResponse.model_validate(item) for item in items],
        page=PageMeta(total=total, limit=limit, offset=offset),
    )


@router.get("/{batch_id}", response_model=ImportBatchResponse)
async def import_get(
    batch_id: uuid.UUID, context: EditorContext, session: DbSession
) -> ImportBatchResponse:
    return ImportBatchResponse.model_validate(
        await service.get_batch(session, context.workspace.id, batch_id)
    )


@router.get("/{batch_id}/rows", response_model=ImportRowPage)
async def import_rows(
    batch_id: uuid.UUID,
    context: EditorContext,
    session: DbSession,
    status: str | None = None,
    has_errors: bool | None = None,
    duplicate: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> ImportRowPage:
    batch = await service.get_batch(session, context.workspace.id, batch_id)
    filters = [ImportRow.batch_id == batch.id]
    if status is not None:
        filters.append(ImportRow.status == status)
    if has_errors is True:
        filters.append(ImportRow.validation_errors.is_not(None))
    elif has_errors is False:
        filters.append(ImportRow.validation_errors.is_(None))
    if duplicate is True:
        filters.append(ImportRow.status == "duplicate")
    elif duplicate is False:
        filters.append(ImportRow.status != "duplicate")
    total = int(
        await session.scalar(select(func.count()).select_from(ImportRow).where(*filters)) or 0
    )
    items = list(
        (
            await session.scalars(
                select(ImportRow)
                .where(*filters)
                .order_by(ImportRow.source_row_number)
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return ImportRowPage(
        items=[ImportRowResponse.model_validate(item) for item in items],
        page=PageMeta(total=total, limit=limit, offset=offset),
    )


@router.patch("/{batch_id}/rows/{row_id}", response_model=ImportRowResponse)
async def import_row_override(
    batch_id: uuid.UUID,
    row_id: uuid.UUID,
    data: ImportRowOverrideRequest,
    context: EditorContext,
    session: DbSession,
) -> ImportRowResponse:
    return ImportRowResponse.model_validate(
        await service.override_duplicate(session, context, batch_id, row_id, data)
    )


@router.put("/{batch_id}/mapping", response_model=ImportBatchResponse)
async def import_mapping(
    batch_id: uuid.UUID,
    data: ImportMappingRequest,
    context: EditorContext,
    session: DbSession,
) -> ImportBatchResponse:
    return ImportBatchResponse.model_validate(
        await service.set_mapping(session, context, batch_id, data)
    )


@router.post("/{batch_id}/validate", response_model=ImportBatchResponse)
async def import_validate(
    batch_id: uuid.UUID, context: EditorContext, session: DbSession
) -> ImportBatchResponse:
    return ImportBatchResponse.model_validate(
        await service.validate_import(session, context, batch_id)
    )


@router.post("/{batch_id}/commit", response_model=ImportActionResponse)
async def import_commit(
    batch_id: uuid.UUID,
    data: ImportCommitRequest,
    context: EditorContext,
    session: DbSession,
    idempotency_key: Annotated[str | None, Header(alias="X-Idempotency-Key")] = None,
) -> ImportActionResponse:
    batch, affected = await service.commit_import(
        session,
        context,
        batch_id,
        confirmation=data.confirm,
        idempotency_key=idempotency_key,
    )
    return ImportActionResponse(
        batch=ImportBatchResponse.model_validate(batch), affected_transactions=affected
    )


@router.post("/{batch_id}/rollback", response_model=ImportActionResponse)
async def import_rollback(
    batch_id: uuid.UUID,
    data: ImportRollbackRequest,
    context: EditorContext,
    session: DbSession,
) -> ImportActionResponse:
    batch, affected = await service.rollback_import(session, context, batch_id, force=data.force)
    return ImportActionResponse(
        batch=ImportBatchResponse.model_validate(batch), affected_transactions=affected
    )


@router.post("/{batch_id}/cancel", response_model=ImportBatchResponse)
async def import_cancel(
    batch_id: uuid.UUID, context: EditorContext, session: DbSession
) -> ImportBatchResponse:
    return ImportBatchResponse.model_validate(
        await service.cancel_import(session, context, batch_id)
    )
