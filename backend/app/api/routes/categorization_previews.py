import uuid

from fastapi import APIRouter, Query

from app.db.models.categorization_previews import (
    CategorizationPreview,
    CategorizationPreviewItem,
)
from app.dependencies.context import CurrentContext
from app.dependencies.database import DbSession
from app.repositories import categorization_previews as repository
from app.schemas.categorization_previews import (
    CategorizationPreviewCreate,
    CategorizationPreviewItemPage,
    CategorizationPreviewItemResponse,
    CategorizationPreviewResponse,
    CategorizationPreviewSummary,
    CategorizationPreviewTransactionSnapshot,
)
from app.schemas.common import PageMeta
from app.services import categorization_previews as service

router = APIRouter()


def _response(preview: CategorizationPreview) -> CategorizationPreviewResponse:
    return CategorizationPreviewResponse(
        id=preview.id,
        workspace_id=preview.workspace_id,
        created_by=preview.created_by,
        rule_set_version=preview.rule_set_version,
        selection_mode=preview.selection_mode,  # type: ignore[arg-type]
        created_at=preview.created_at,
        expires_at=preview.expires_at,
        summary=CategorizationPreviewSummary(
            selected=preview.selected_count,
            matched=preview.matched_count,
            no_match=preview.no_match_count,
            transfer=preview.transfer_count,
            already_categorized=preview.already_categorized_count,
            split=preview.split_count,
            reconciled=preview.reconciled_count,
            closed_period=preview.closed_period_count,
            not_found=preview.not_found_count,
        ),
    )


def _item_response(item: CategorizationPreviewItem) -> CategorizationPreviewItemResponse:
    snapshot = item.transaction_snapshot
    return CategorizationPreviewItemResponse(
        id=item.id,
        sequence=item.sequence,
        transaction_id=item.transaction_id,
        transaction_version=item.transaction_version,
        status=item.status,  # type: ignore[arg-type]
        transaction=(
            CategorizationPreviewTransactionSnapshot.model_validate(snapshot)
            if snapshot is not None
            else None
        ),
        rule_id=item.rule_id,
        rule_version=item.rule_version,
        rule_name=item.rule_name,
        category_id=item.category_id,
        category_version=item.category_version,
        category_name=item.category_name,
    )


@router.post("", response_model=CategorizationPreviewResponse, status_code=201)
async def categorization_preview_create(
    data: CategorizationPreviewCreate,
    context: CurrentContext,
    session: DbSession,
) -> CategorizationPreviewResponse:
    """Build and persist a preview.

    Any current workspace member may create one, matching the existing single-preview capability:
    preview never mutates financial data. Bulk apply will require editor or owner in Stage A3.
    """
    return _response(await service.create_preview(session, context, data))


@router.get("/{preview_id}", response_model=CategorizationPreviewResponse)
async def categorization_preview_get(
    preview_id: uuid.UUID,
    context: CurrentContext,
    session: DbSession,
) -> CategorizationPreviewResponse:
    return _response(await service.get_preview(session, context.workspace.id, preview_id))


@router.get("/{preview_id}/items", response_model=CategorizationPreviewItemPage)
async def categorization_preview_items(
    preview_id: uuid.UUID,
    context: CurrentContext,
    session: DbSession,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> CategorizationPreviewItemPage:
    preview = await service.get_preview(session, context.workspace.id, preview_id)
    items, total = await repository.list_items(session, preview.id, limit=limit, offset=offset)
    return CategorizationPreviewItemPage(
        items=[_item_response(item) for item in items],
        page=PageMeta(limit=limit, offset=offset, total=total),
    )
