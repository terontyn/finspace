import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.categorization_apply_operations import CategorizationApplyOperation
from app.db.models.categorization_previews import CategorizationPreview, CategorizationPreviewItem
from app.db.models.transactions import FinancialTransaction, TransactionSplit
from app.db.models.users import Workspace

# Detail loading is chunked so a large selection never becomes one enormous IN list.
DETAIL_CHUNK = 500


@dataclass(slots=True)
class PreviewCandidate:
    """Compact transaction state evaluated by one PostgreSQL statement snapshot."""

    id: uuid.UUID
    version: int
    occurred_at: datetime
    transaction_type: str
    amount: Decimal
    currency: str
    account_id: uuid.UUID
    payee_id: uuid.UUID | None
    counterparty: str | None
    description: str | None
    status: str
    source: str
    category_id: uuid.UUID | None
    has_splits: bool


def candidate_filters(
    workspace_id: uuid.UUID,
    *,
    import_batch_id: uuid.UUID | None,
    occurred_from: datetime | None,
    occurred_to: datetime | None,
    account_id: uuid.UUID | None,
    payee_id: uuid.UUID | None,
    transaction_type: str | None,
    status: str | None,
    source: str | None,
) -> list[Any]:
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
    if import_batch_id is not None:
        filters.append(FinancialTransaction.import_batch_id == import_batch_id)
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


def _candidate_statement(filters: list[Any]) -> Select[Any]:
    split_exists = (
        exists(
            select(TransactionSplit.id).where(
                TransactionSplit.transaction_id == FinancialTransaction.id
            )
        )
        .correlate(FinancialTransaction)
        .label("has_splits")
    )
    return select(
        FinancialTransaction.id.label("transaction_id"),
        FinancialTransaction.version.label("transaction_version"),
        FinancialTransaction.occurred_at,
        FinancialTransaction.transaction_type,
        FinancialTransaction.amount,
        FinancialTransaction.currency,
        FinancialTransaction.account_id,
        FinancialTransaction.payee_id,
        FinancialTransaction.counterparty,
        FinancialTransaction.description,
        FinancialTransaction.status,
        FinancialTransaction.source,
        FinancialTransaction.category_id,
        split_exists,
    ).where(*filters)


def _candidate_rows(result: Sequence[Any]) -> list[PreviewCandidate]:
    return [
        PreviewCandidate(
            id=row.transaction_id,
            version=row.transaction_version,
            occurred_at=row.occurred_at,
            transaction_type=row.transaction_type,
            amount=row.amount,
            currency=row.currency,
            account_id=row.account_id,
            payee_id=row.payee_id,
            counterparty=row.counterparty,
            description=row.description,
            status=row.status,
            source=row.source,
            category_id=row.category_id,
            has_splits=bool(row.has_splits),
        )
        for row in result
    ]


async def filtered_candidates(
    session: AsyncSession,
    filters: list[Any],
    *,
    limit: int,
) -> list[PreviewCandidate]:
    """Return filter membership and classification inputs from one statement snapshot.

    Ordering is the canonical transaction order (``occurred_at DESC, id DESC``). Because the same
    bounded statement returns both the selected set and ``has_splits``, a concurrent update cannot
    combine an old transaction version with new split state or turn a selected filter row into a
    synthetic ``not_found`` item.
    """
    statement: Select[Any] = (
        _candidate_statement(filters)
        .order_by(FinancialTransaction.occurred_at.desc(), FinancialTransaction.id.desc())
        .limit(limit)
    )
    return _candidate_rows((await session.execute(statement)).all())


async def explicit_candidates(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    transaction_ids: list[uuid.UUID],
) -> dict[uuid.UUID, PreviewCandidate]:
    """Load workspace-visible explicit IDs with atomic row/split state per bounded statement."""
    loaded: dict[uuid.UUID, PreviewCandidate] = {}
    for start in range(0, len(transaction_ids), DETAIL_CHUNK):
        chunk = transaction_ids[start : start + DETAIL_CHUNK]
        statement = _candidate_statement(
            [
                FinancialTransaction.workspace_id == workspace_id,
                FinancialTransaction.deleted_at.is_(None),
                FinancialTransaction.id.in_(chunk),
            ]
        )
        for candidate in _candidate_rows((await session.execute(statement)).all()):
            loaded[candidate.id] = candidate
    return loaded


async def get_preview(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    preview_id: uuid.UUID,
    *,
    for_share: bool = False,
) -> CategorizationPreview | None:
    statement = select(CategorizationPreview).where(
        CategorizationPreview.id == preview_id,
        CategorizationPreview.workspace_id == workspace_id,
    )
    if for_share:
        # A new apply claim holds this lock through operation insertion. Expiry cleanup takes the
        # conflicting FOR UPDATE lock, so either the claim becomes visible first or cleanup removes
        # the row before the apply can validate it.
        statement = statement.with_for_update(read=True)
    return await session.scalar(statement)


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
    recovery_window: timedelta,
    limit: int = 100,
) -> int:
    """Bounded, operation-aware physical pruning of previews whose TTL has passed.

    Candidate preview rows are owned with ``FOR UPDATE SKIP LOCKED`` before deletion. A new apply
    claim holds ``FOR SHARE`` on the same row until its ``in_progress`` operation is committed, so
    cleanup either skips that locked preview or wins first and deletes it before the apply can
    validate it. That lock protects a genuinely active claim regardless of how old the operation
    is. Completed operations do not pin previews because their terminal results are independently
    replayable.

    A committed in-progress operation pins its preview through the anti-exists predicate, but only
    until ``expires_at + recovery_window``. The window is anchored on the preview's own expiry
    rather than on an operation timestamp, so there is one lifecycle clock with an offset instead
    of two, and the maximum physical retention of any preview is a single documented constant. The
    boundary is inclusive: at ``expires_at == now - recovery_window`` the preview is eligible.

    Nothing about financial safety depends on this pin. A committed apply result and the mutation
    it describes share one transaction, so a same-key retry replays terminal results rather than
    re-executing them whether or not the preview still exists; after reclamation an item that was
    never attempted resolves to ``not_found``.

    ``now`` and ``recovery_window`` are both supplied by the caller, so pruning never reads the
    clock or the settings object and stays deterministic under test.
    """
    in_progress_operation = exists(
        select(CategorizationApplyOperation.id).where(
            CategorizationApplyOperation.workspace_id == CategorizationPreview.workspace_id,
            CategorizationApplyOperation.preview_id == CategorizationPreview.id,
            CategorizationApplyOperation.status == "in_progress",
        )
    )
    # Written against the bare column so both branches stay eligible for
    # ix_categorization_previews_workspace_expiry; the cutoff is a plain bound parameter.
    recovery_elapsed = CategorizationPreview.expires_at <= now - recovery_window
    expired = list(
        (
            await session.scalars(
                select(CategorizationPreview.id)
                .where(
                    CategorizationPreview.workspace_id == workspace_id,
                    CategorizationPreview.expires_at <= now,
                    or_(~in_progress_operation, recovery_elapsed),
                )
                .order_by(CategorizationPreview.expires_at, CategorizationPreview.id)
                .limit(limit)
                .with_for_update(of=CategorizationPreview, skip_locked=True)
            )
        ).all()
    )
    if not expired:
        return 0
    await session.execute(
        delete(CategorizationPreview).where(CategorizationPreview.id.in_(expired))
    )
    return len(expired)


async def list_workspace_ids_after(
    session: AsyncSession,
    *,
    after_id: uuid.UUID | None,
    limit: int,
) -> list[uuid.UUID]:
    """One bounded keyset page of workspace identifiers, ordered by primary key.

    Maintenance sweeps use this instead of loading the workspace table: the pruning predicate is
    workspace-scoped, so the worker walks workspaces a page at a time and never holds more than one
    page in memory. No locks are taken — the page is only a work list, and every prune decision is
    re-made under the row locks ``delete_expired`` acquires.
    """
    statement = select(Workspace.id).order_by(Workspace.id).limit(limit)
    if after_id is not None:
        statement = statement.where(Workspace.id > after_id)
    return list((await session.scalars(statement)).all())
