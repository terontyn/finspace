import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.categorization_apply_operations import (
    CategorizationApplyOperation,
    CategorizationApplyResult,
)
from app.db.models.categorization_previews import CategorizationPreviewItem
from app.db.models.users import User


@dataclass(frozen=True, slots=True)
class HistoryOperationRow:
    operation: CategorizationApplyOperation
    actor_display_name: str | None


async def list_history_operations(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> tuple[list[HistoryOperationRow], int]:
    """Return one deterministic workspace page with a bounded actor lookup."""
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(CategorizationApplyOperation)
            .where(CategorizationApplyOperation.workspace_id == workspace_id)
        )
        or 0
    )
    rows = (
        await session.execute(
            select(CategorizationApplyOperation, User.display_name)
            .outerjoin(
                User,
                and_(
                    User.id == CategorizationApplyOperation.actor_user_id,
                    User.deleted_at.is_(None),
                ),
            )
            .where(CategorizationApplyOperation.workspace_id == workspace_id)
            .order_by(
                CategorizationApplyOperation.created_at.desc(),
                CategorizationApplyOperation.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [HistoryOperationRow(row[0], row[1]) for row in rows], total


async def history_result_counts(
    session: AsyncSession,
    operation_ids: list[uuid.UUID],
) -> dict[uuid.UUID, dict[str, int]]:
    """Aggregate an entire bounded operation page in one query, never one query per row."""
    if not operation_ids:
        return {}
    rows = (
        await session.execute(
            select(
                CategorizationApplyResult.operation_id,
                CategorizationApplyResult.status,
                func.count(),
            )
            .where(CategorizationApplyResult.operation_id.in_(operation_ids))
            .group_by(CategorizationApplyResult.operation_id, CategorizationApplyResult.status)
        )
    ).all()
    counts: dict[uuid.UUID, dict[str, int]] = {}
    for operation_id, status, count in rows:
        counts.setdefault(operation_id, {})[status] = int(count)
    return counts


async def get_history_operation(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    operation_id: uuid.UUID,
) -> HistoryOperationRow | None:
    row = (
        await session.execute(
            select(CategorizationApplyOperation, User.display_name)
            .outerjoin(
                User,
                and_(
                    User.id == CategorizationApplyOperation.actor_user_id,
                    User.deleted_at.is_(None),
                ),
            )
            .where(
                CategorizationApplyOperation.workspace_id == workspace_id,
                CategorizationApplyOperation.id == operation_id,
            )
        )
    ).one_or_none()
    return HistoryOperationRow(row[0], row[1]) if row is not None else None


async def list_history_results(
    session: AsyncSession,
    operation_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> tuple[list[CategorizationApplyResult], int]:
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(CategorizationApplyResult)
            .where(CategorizationApplyResult.operation_id == operation_id)
        )
        or 0
    )
    rows = list(
        (
            await session.scalars(
                select(CategorizationApplyResult)
                .where(CategorizationApplyResult.operation_id == operation_id)
                .order_by(CategorizationApplyResult.sequence)
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return rows, total


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
