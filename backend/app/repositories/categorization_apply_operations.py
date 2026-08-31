import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.categorization_apply_operations import (
    CategorizationApplyOperation,
    CategorizationApplyResult,
)
from app.db.models.categorization_previews import CategorizationPreviewItem


async def claim_operation(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    preview_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    idempotency_key: str,
    request_hash: str,
    requested_count: int,
) -> CategorizationApplyOperation:
    """Create the operation, or return the one this idempotency key already claimed.

    Insert-on-conflict makes two concurrent requests carrying the same key converge on one logical
    operation instead of racing to create two.
    """
    await session.execute(
        insert(CategorizationApplyOperation)
        .values(
            workspace_id=workspace_id,
            preview_id=preview_id,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            status="in_progress",
            requested_count=requested_count,
        )
        .on_conflict_do_nothing(
            index_elements=[
                CategorizationApplyOperation.workspace_id,
                CategorizationApplyOperation.idempotency_key,
            ]
        )
    )
    operation = await find_operation(session, workspace_id, idempotency_key)
    if operation is None:
        raise RuntimeError("Categorization apply operation could not be created")
    return operation


async def find_operation(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    idempotency_key: str,
) -> CategorizationApplyOperation | None:
    return await session.scalar(
        select(CategorizationApplyOperation)
        .where(
            CategorizationApplyOperation.workspace_id == workspace_id,
            CategorizationApplyOperation.idempotency_key == idempotency_key,
        )
        .execution_options(populate_existing=True)
    )


async def results_for(
    session: AsyncSession,
    operation_id: uuid.UUID,
) -> dict[uuid.UUID, CategorizationApplyResult]:
    rows = await session.scalars(
        select(CategorizationApplyResult)
        .where(CategorizationApplyResult.operation_id == operation_id)
        .execution_options(populate_existing=True)
    )
    return {row.item_id: row for row in rows.all()}


def build_result(
    *,
    operation_id: uuid.UUID,
    item_id: uuid.UUID,
    sequence: int,
    transaction_id: uuid.UUID | None,
    status: str,
    error_code: str | None,
    expected_version: int | None = None,
    current_version: int | None = None,
) -> CategorizationApplyResult:
    return CategorizationApplyResult(
        operation_id=operation_id,
        item_id=item_id,
        sequence=sequence,
        transaction_id=transaction_id,
        status=status,
        error_code=error_code,
        expected_version=expected_version,
        current_version=current_version,
    )


async def load_items(
    session: AsyncSession,
    preview_id: uuid.UUID,
    item_ids: list[uuid.UUID],
) -> dict[uuid.UUID, CategorizationPreviewItem]:
    rows = await session.scalars(
        select(CategorizationPreviewItem).where(
            CategorizationPreviewItem.preview_id == preview_id,
            CategorizationPreviewItem.id.in_(item_ids),
        )
    )
    return {row.id: row for row in rows.all()}


async def complete_operation(
    session: AsyncSession,
    operation_id: uuid.UUID,
    completed_at: datetime,
) -> None:
    operation = await session.get(CategorizationApplyOperation, operation_id)
    if operation is None or operation.status == "completed":
        return
    operation.status = "completed"
    operation.completed_at = completed_at
