"""Read-only operational history for persisted bulk categorization apply attempts.

History deliberately reads only the apply ledger and current actor profile. It never loads or
reinterprets transactions, rules, categories, previews or preview items, and it never resumes an
in-progress operation. Apply operations/results currently have no retention policy; this API does
not promise permanent archival semantics.
"""

import uuid
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models.categorization_apply_operations import (
    APPLY_RESULT_STATUSES,
)
from app.repositories import categorization_apply_operations as repository
from app.schemas.categorization_apply import (
    CategorizationApplyHistoryActor,
    CategorizationApplyHistoryCounts,
    CategorizationApplyHistoryResult,
    CategorizationApplyOperationHistory,
    CategorizationApplyOperationHistoryDetail,
    CategorizationApplyOperationStatus,
    CategorizationApplyStatus,
)
from app.schemas.common import PageMeta


def _not_found() -> ApiError:
    return ApiError(
        status_code=404,
        code="CATEGORIZATION_APPLY_OPERATION_NOT_FOUND",
        message="Categorization apply operation was not found",
    )


def _counts(raw: dict[str, int]) -> CategorizationApplyHistoryCounts:
    return CategorizationApplyHistoryCounts.model_validate(
        {status: int(raw.get(status, 0)) for status in APPLY_RESULT_STATUSES}
    )


def _operation_response(
    row: repository.HistoryOperationRow,
    raw_counts: dict[str, int],
) -> CategorizationApplyOperationHistory:
    operation = row.operation
    counts = _counts(raw_counts)
    return CategorizationApplyOperationHistory(
        id=operation.id,
        actor=CategorizationApplyHistoryActor(
            actor_user_id=operation.actor_user_id,
            display_name=row.actor_display_name,
        ),
        status=cast(CategorizationApplyOperationStatus, operation.status),
        requested_count=operation.requested_count,
        result_count=sum(getattr(counts, status) for status in APPLY_RESULT_STATUSES),
        counts=counts,
        created_at=operation.created_at,
        completed_at=operation.completed_at,
    )


async def list_operations(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> tuple[list[CategorizationApplyOperationHistory], int]:
    rows, total = await repository.list_history_operations(
        session,
        workspace_id,
        limit=limit,
        offset=offset,
    )
    raw_counts = await repository.history_result_counts(
        session,
        [row.operation.id for row in rows],
    )
    return [_operation_response(row, raw_counts.get(row.operation.id, {})) for row in rows], total


async def get_operation_detail(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    operation_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> CategorizationApplyOperationHistoryDetail:
    row = await repository.get_history_operation(session, workspace_id, operation_id)
    if row is None:
        raise _not_found()
    results, total = await repository.list_history_results(
        session,
        operation_id,
        limit=limit,
        offset=offset,
    )
    raw_counts = (await repository.history_result_counts(session, [operation_id])).get(
        operation_id, {}
    )
    operation = _operation_response(row, raw_counts)
    return CategorizationApplyOperationHistoryDetail(
        **operation.model_dump(),
        results=[
            CategorizationApplyHistoryResult(
                sequence=result.sequence,
                transaction_id=result.transaction_id,
                status=cast(CategorizationApplyStatus, result.status),
                error_code=result.error_code,
                expected_version=result.expected_version,
                current_version=result.current_version,
                created_at=result.created_at,
            )
            for result in results
        ],
        page=PageMeta(limit=limit, offset=offset, total=total),
    )
