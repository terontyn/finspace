import uuid
from datetime import datetime

from fastapi import APIRouter, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies.context import EditorContext, RequestContext
from app.dependencies.database import DbSession
from app.schemas.audit import AuditPage, AuditResponse
from app.schemas.common import PageMeta
from app.services.audit import list_audit_entries

router = APIRouter()


async def _audit_page(
    session: AsyncSession,
    context: RequestContext,
    *,
    entity_type: str | None,
    entity_id: uuid.UUID | None,
    date_from: datetime | None,
    date_to: datetime | None,
    limit: int,
    offset: int,
) -> AuditPage:
    items, total = await list_audit_entries(
        session,
        context.workspace.id,
        entity_type=entity_type,
        entity_id=entity_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return AuditPage(
        items=[AuditResponse.model_validate(item) for item in items],
        page=PageMeta(limit=limit, offset=offset, total=total),
    )


@router.get("", response_model=AuditPage)
async def audit_list(
    context: EditorContext,
    session: DbSession,
    entity_type: str | None = Query(default=None, max_length=100),
    entity_id: uuid.UUID | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AuditPage:
    return await _audit_page(
        session,
        context,
        entity_type=entity_type,
        entity_id=entity_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
