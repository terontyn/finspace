"""Persisted bulk categorization preview construction.

Stage A2 is preview only: nothing here mutates a transaction. Construction is synchronous and
bounded by hard selection caps, so no background worker is involved.

Concurrency model:

* the Stage A1 workspace rule-set control is held ``FOR SHARE`` for the whole construction
  transaction, so the deterministic rule set cannot change halfway through and rule mutations
  serialize behind the preview;
* candidate membership, compact transaction state and split existence come from the same bounded
  PostgreSQL statement snapshot. Individual transactions remain optimistic and may change after
  that read, which is why every item persists the exact version it evaluated;
* the persisted proposal is immutable afterwards. Stage A3 apply revalidates live state.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models.categorization_previews import CategorizationPreview, CategorizationPreviewItem
from app.dependencies.context import RequestContext
from app.repositories import categorization_previews as repository
from app.repositories import categorization_rules as rule_repository
from app.schemas.categorization_previews import (
    MAX_FILTER_CANDIDATES,
    CategorizationPreviewCreate,
    CategorizationPreviewFilterSelection,
    CategorizationPreviewIdsSelection,
)
from app.services.categorization_matcher import (
    MatchCandidate,
    PreparedMatch,
    PreparedRuleSet,
    prepare_rule_set,
)
from app.services.financial_period_guard import closed_dates, get_or_create_control

PREVIEW_TTL = timedelta(hours=24)

# Persisted classification precedence. The first condition that holds wins.
STATUS_ORDER = (
    "not_found",
    "transfer",
    "already_categorized",
    "split",
    "reconciled",
    "closed_period",
    "matched",
    "no_match",
)


@dataclass
class _Counts:
    selected: int = 0
    matched: int = 0
    no_match: int = 0
    transfer: int = 0
    already_categorized: int = 0
    split: int = 0
    reconciled: int = 0
    closed_period: int = 0
    not_found: int = 0

    def record(self, status: str) -> None:
        setattr(self, status, getattr(self, status) + 1)
        self.selected += 1


def _not_found() -> ApiError:
    return ApiError(
        status_code=404,
        code="CATEGORIZATION_PREVIEW_NOT_FOUND",
        message="Categorization preview was not found",
    )


def _snapshot(transaction: repository.PreviewCandidate) -> dict[str, object]:
    return {
        "transaction_id": str(transaction.id),
        "version": transaction.version,
        "occurred_at": transaction.occurred_at.isoformat(),
        "transaction_type": transaction.transaction_type,
        "amount": str(transaction.amount),
        "currency": transaction.currency,
        "account_id": str(transaction.account_id),
        "payee_id": str(transaction.payee_id) if transaction.payee_id else None,
        "counterparty": transaction.counterparty,
        "description": transaction.description,
        "status": transaction.status,
        "source": transaction.source,
    }


def _classify(
    transaction: repository.PreviewCandidate,
    *,
    has_splits: bool,
    is_closed: bool,
    rule_set: PreparedRuleSet,
) -> tuple[str, PreparedMatch | None]:
    """Apply the fixed precedence and, for eligible transactions, the deterministic match."""
    if transaction.transaction_type == "transfer":
        return "transfer", None
    if transaction.category_id is not None:
        return "already_categorized", None
    if has_splits:
        return "split", None
    if transaction.status == "reconciled":
        return "reconciled", None
    if is_closed:
        return "closed_period", None
    match = rule_set.match(MatchCandidate.from_transaction(transaction))
    if match is None:
        return "no_match", None
    return "matched", match


async def _resolve_selection(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    data: CategorizationPreviewCreate,
) -> tuple[list[uuid.UUID], dict[uuid.UUID, repository.PreviewCandidate]]:
    selection = data.selection
    if isinstance(selection, CategorizationPreviewIdsSelection):
        # Caller order is preserved so the reviewer sees the list they submitted.
        selected_ids = list(selection.transaction_ids)
        return selected_ids, await repository.explicit_candidates(
            session,
            workspace_id,
            selected_ids,
        )
    if not isinstance(selection, CategorizationPreviewFilterSelection):
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="Unsupported categorization preview selection",
        )
    filters = repository.candidate_filters(
        workspace_id,
        occurred_from=selection.occurred_from,
        occurred_to=selection.occurred_to,
        account_id=selection.account_id,
        payee_id=selection.payee_id,
        transaction_type=selection.transaction_type,
        status=selection.status,
        source=selection.source,
    )
    # limit + 1 detects overflow without counting or scanning the whole table.
    candidates = await repository.filtered_candidates(
        session,
        filters,
        limit=MAX_FILTER_CANDIDATES + 1,
    )
    if len(candidates) > MAX_FILTER_CANDIDATES:
        raise ApiError(
            status_code=422,
            code="CATEGORIZATION_PREVIEW_LIMIT_EXCEEDED",
            message="The selection matches more transactions than one preview may hold",
            details={"maximum": MAX_FILTER_CANDIDATES},
        )
    selected_ids = [candidate.id for candidate in candidates]
    return selected_ids, {candidate.id: candidate for candidate in candidates}


async def create_preview(
    session: AsyncSession,
    context: RequestContext,
    data: CategorizationPreviewCreate,
) -> CategorizationPreview:
    workspace_id = context.workspace.id
    # Shared Stage A1 gate: rule mutations wait behind it, other previews and applies do not.
    control = await rule_repository.get_or_create_rule_set_control(
        session,
        workspace_id,
        for_share=True,
    )
    rule_set_version = control.version
    rule_set = await prepare_rule_set(session, workspace_id)

    selected_ids, transactions = await _resolve_selection(session, workspace_id, data)

    # Advisory only: month-close state is read after the candidate statement without taking the
    # exclusive control lock. Its independent read point does not weaken the atomic transaction /
    # split candidate snapshot; Stage A3 apply revalidates month-close state authoritatively.
    month_control = await get_or_create_control(session, workspace_id, for_update=False)
    timezone = context.workspace.timezone

    now = datetime.now(UTC)
    preview = CategorizationPreview(
        workspace_id=workspace_id,
        created_by=context.user.id,
        rule_set_version=rule_set_version,
        selection_mode=data.selection.mode,
        selection=data.selection.model_dump(mode="json"),
        created_at=now,
        expires_at=now + PREVIEW_TTL,
    )
    session.add(preview)
    await session.flush()

    counts = _Counts()
    items: list[CategorizationPreviewItem] = []
    for sequence, transaction_id in enumerate(selected_ids):
        transaction = transactions.get(transaction_id)
        if transaction is None:
            # Missing and foreign-workspace identifiers are indistinguishable by design.
            counts.record("not_found")
            items.append(
                CategorizationPreviewItem(
                    preview_id=preview.id,
                    sequence=sequence,
                    transaction_id=transaction_id,
                    transaction_version=None,
                    status="not_found",
                    transaction_snapshot=None,
                )
            )
            continue
        is_closed = bool(closed_dates(month_control, timezone, [transaction.occurred_at]))
        status, match = _classify(
            transaction,
            has_splits=transaction.has_splits,
            is_closed=is_closed,
            rule_set=rule_set,
        )
        counts.record(status)
        item = CategorizationPreviewItem(
            preview_id=preview.id,
            sequence=sequence,
            transaction_id=transaction.id,
            transaction_version=transaction.version,
            status=status,
            transaction_snapshot=_snapshot(transaction),
        )
        if status == "matched" and match is not None:
            item.rule_id = match.rule.id
            item.rule_version = match.rule.version
            item.rule_name = match.rule.name
            item.category_id = match.category.id
            item.category_version = match.category.version
            item.category_name = match.category.name
        items.append(item)

    session.add_all(items)
    preview.selected_count = counts.selected
    preview.matched_count = counts.matched
    preview.no_match_count = counts.no_match
    preview.transfer_count = counts.transfer
    preview.already_categorized_count = counts.already_categorized
    preview.split_count = counts.split
    preview.reconciled_count = counts.reconciled
    preview.closed_period_count = counts.closed_period
    preview.not_found_count = counts.not_found
    # Header, items and summary commit together: a partially built preview is never visible.
    await session.commit()
    await session.refresh(preview)
    return preview


async def get_preview(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    preview_id: uuid.UUID,
) -> CategorizationPreview:
    """Workspace-scoped read with logical TTL enforcement.

    A preview from another workspace is indistinguishable from a missing one (404). An expired
    preview belonging to this workspace reports 410 so the caller can tell the difference.
    """
    preview = await repository.get_preview(session, workspace_id, preview_id)
    if preview is None:
        raise _not_found()
    if preview.expires_at <= datetime.now(UTC):
        raise ApiError(
            status_code=410,
            code="CATEGORIZATION_PREVIEW_EXPIRED",
            message="Categorization preview has expired",
            details={"expires_at": preview.expires_at.isoformat()},
        )
    return preview
