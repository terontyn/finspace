import uuid
from datetime import datetime

from sqlalchemy import Select, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.categorization_previews import CategorizationPreview, CategorizationPreviewItem
from app.db.models.transactions import FinancialTransaction, TransactionSplit

# Detail loading is chunked so a large selection never becomes one enormous IN list.
DETAIL_CHUNK = 500


def candidate_filters(
    workspace_id: uuid.UUID,
    *,
    occurred_from: datetime | None,
    occurred_to: datetime | None,
    account_id: uuid.UUID | None,
    payee_id: uuid.UUID | None,
    transaction_type: str | None,
    status: str | None,
    source: str | None,
) -> list:
    """Filter-mode candidate predicate.

    Mirrors ``transactions.list_transactions`` semantics (including the account filter matching
    either side of a transfer) and additionally restricts to uncategorized transactions, because
    bulk preview never proposes overwriting an existing category.
    """
    filters = [
        FinancialTransaction.workspace_id == workspace_id,
        FinancialTransaction.deleted_at.is_(None),
        FinancialTransaction.category_id.is_(None),
    ]
    if occurred_from is not None:
        filters.append(FinancialTransaction.occurred_at >= occurred_from)
    if occurred_to is not None:
        filters.append(FinancialTransaction.occurred_at <= occurred_to)
    if transaction_type is not None:
        filters.append(FinancialTransaction.transaction_type == transaction_type)
    if status is not None:
        filters.append(FinancialTransaction.status == status)
    if source is not None:
        filters.append(FinancialTransaction.source == source)
    if account_id is not None:
        filters.append(
            or_(
                FinancialTransaction.account_id == account_id,
                FinancialTransaction.target_account_id == account_id,
            )
        )
    if payee_id is not None:
        filters.append(FinancialTransaction.payee_id == payee_id)
    return filters


async def candidate_ids(
    session: AsyncSession,
    filters: list,
    *,
    limit: int,
) -> list[uuid.UUID]:
    """Fix candidate membership in one bounded query before any chunked detail loading.

    Ordering is the canonical transaction order (``occurred_at DESC, id DESC``). Selecting the ids
    up front means a concurrent ``occurred_at`` change cannot make a later keyset scan skip or
    duplicate rows.
    """
    statement: Select = (
        select(FinancialTransaction.id)
        .where(*filters)
        .order_by(FinancialTransaction.occurred_at.desc(), FinancialTransaction.id.desc())
        .limit(limit)
    )
    return list((await session.scalars(statement)).all())


async def load_transactions(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    transaction_ids: list[uuid.UUID],
) -> dict[uuid.UUID, FinancialTransaction]:
    """Load the fixed candidate set in bounded chunks, workspace-scoped."""
    loaded: dict[uuid.UUID, FinancialTransaction] = {}
    for start in range(0, len(transaction_ids), DETAIL_CHUNK):
        chunk = transaction_ids[start : start + DETAIL_CHUNK]
        rows = await session.scalars(
            select(FinancialTransaction).where(
                FinancialTransaction.workspace_id == workspace_id,
                FinancialTransaction.deleted_at.is_(None),
                FinancialTransaction.id.in_(chunk),
            )
        )
        for transaction in rows.all():
            loaded[transaction.id] = transaction
    return loaded


async def split_counts(
    session: AsyncSession,
    transaction_ids: list[uuid.UUID],
) -> dict[uuid.UUID, int]:
    """One aggregate query per chunk instead of ``get_splits`` per transaction."""
    counts: dict[uuid.UUID, int] = {}
    for start in range(0, len(transaction_ids), DETAIL_CHUNK):
        chunk = transaction_ids[start : start + DETAIL_CHUNK]
        rows = await session.execute(
            select(TransactionSplit.transaction_id, func.count())
            .where(TransactionSplit.transaction_id.in_(chunk))
            .group_by(TransactionSplit.transaction_id)
        )
        for transaction_id, count in rows.all():
            counts[transaction_id] = int(count)
    return counts


async def get_preview(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    preview_id: uuid.UUID,
) -> CategorizationPreview | None:
    return await session.scalar(
        select(CategorizationPreview).where(
            CategorizationPreview.id == preview_id,
            CategorizationPreview.workspace_id == workspace_id,
        )
    )


async def list_items(
    session: AsyncSession,
    preview_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> tuple[list[CategorizationPreviewItem], int]:
    """Page by persisted ``sequence`` so paging is stable regardless of live transaction changes."""
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(CategorizationPreviewItem)
            .where(CategorizationPreviewItem.preview_id == preview_id)
        )
        or 0
    )
    items = list(
        (
            await session.scalars(
                select(CategorizationPreviewItem)
                .where(CategorizationPreviewItem.preview_id == preview_id)
                .order_by(CategorizationPreviewItem.sequence)
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return items, total


async def delete_expired(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    now: datetime,
    *,
    limit: int = 100,
) -> int:
    """Bounded physical pruning of previews whose TTL has passed.

    Stage A2 enforces the TTL logically on read; this is the reusable cleanup primitive. No
    scheduler invokes it yet — scheduled pruning is deferred to later hardening.
    """
    expired = list(
        (
            await session.scalars(
                select(CategorizationPreview.id)
                .where(
                    CategorizationPreview.workspace_id == workspace_id,
                    CategorizationPreview.expires_at <= now,
                )
                .limit(limit)
            )
        ).all()
    )
    if not expired:
        return 0
    await session.execute(
        delete(CategorizationPreview).where(CategorizationPreview.id.in_(expired))
    )
    return len(expired)
