"""Idempotent bulk application of persisted categorization preview items.

Shape of the request:

* one database transaction **per item**, never one for the whole request, so a conflict on one item
  cannot roll back unrelated successful items;
* each item's transaction atomically contains the transaction mutation, its version bump, its audit
  entry, its sync-outbox row and the persisted terminal result — a committed mutation and its
  recorded result can never disagree;
* the persisted result, not the transaction version, is the authority for idempotency.

Recovery: the operation row is claimed before any item is processed and completed only once every
requested item has a terminal result. A process that dies mid-request therefore leaves an operation
whose finished items are terminal and whose remaining items were simply never attempted; retrying
with the same idempotency key replays the former and processes only the latter. No background worker
is involved. A new claim holds ``FOR SHARE`` on its live preview until the operation commits;
physical expiry cleanup takes ``FOR UPDATE`` on that same row and retains previews referenced by a
committed in-progress operation. Completed operations need only their independently persisted
results and remain replayable after physical preview pruning.
"""

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models.categorization_apply_operations import CategorizationApplyResult
from app.db.models.categorization_previews import CategorizationPreviewItem
from app.dependencies.context import RequestContext
from app.repositories import categorization_apply_operations as repository
from app.repositories import categorization_previews as preview_repository
from app.schemas.categorization_apply import CategorizationApplyRequest
from app.services import categorization_executor as executor
from app.services import categorization_previews as preview_service

logger = logging.getLogger(__name__)

# A preview item that was not proposed for application can never become one here. Its recorded
# preview status maps straight onto a terminal apply result without touching the transaction.
INELIGIBLE_PREVIEW_STATUS: dict[str, str] = {
    "no_match": executor.NO_MATCH,
    "already_categorized": executor.ALREADY_CATEGORIZED,
    "split": executor.SPLIT,
    "transfer": executor.TRANSFER,
    "reconciled": executor.RECONCILED,
    "closed_period": executor.CLOSED_PERIOD,
    "not_found": executor.NOT_FOUND,
}

CONFLICT_STATUSES = frozenset(
    {
        executor.TRANSACTION_CHANGED,
        executor.RULE_CHANGED,
        executor.CATEGORY_CHANGED,
    }
)


@dataclass(frozen=True)
class ItemResult:
    item_id: uuid.UUID
    transaction_id: uuid.UUID | None
    status: str
    error_code: str | None
    expected_version: int | None
    current_version: int | None


@dataclass(frozen=True)
class BulkApplyOutcome:
    preview_id: uuid.UUID
    operation_id: uuid.UUID
    results: list[ItemResult]


def canonical_request_hash(
    workspace_id: uuid.UUID,
    preview_id: uuid.UUID,
    item_ids: list[uuid.UUID],
) -> str:
    """Deterministic identity of a logical apply request.

    Item identifiers are treated as a **set**: they are sorted by their canonical string form before
    hashing, so the same items submitted in a different order are the same logical request. Nothing
    ephemeral (request id, timestamp, actor) participates. Response ordering is separate and follows
    the caller's submitted order.
    """
    payload = json.dumps(
        {
            "workspace_id": str(workspace_id),
            "preview_id": str(preview_id),
            "item_ids": sorted(str(item) for item in item_ids),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _idempotency_conflict() -> ApiError:
    return ApiError(
        status_code=409,
        code="CATEGORIZATION_IDEMPOTENCY_CONFLICT",
        message="This idempotency key was already used for a different request",
    )


def _from_row(row: CategorizationApplyResult) -> ItemResult:
    return ItemResult(
        item_id=row.item_id,
        transaction_id=row.transaction_id,
        status=row.status,
        error_code=row.error_code,
        expected_version=row.expected_version,
        current_version=row.current_version,
    )


async def _persist(
    session: AsyncSession,
    *,
    operation_id: uuid.UUID,
    item_id: uuid.UUID,
    sequence: int,
    transaction_id: uuid.UUID | None,
    status: str,
    expected_version: int | None = None,
    current_version: int | None = None,
) -> None:
    session.add(
        repository.build_result(
            operation_id=operation_id,
            item_id=item_id,
            sequence=sequence,
            transaction_id=transaction_id,
            status=status,
            error_code=executor.ERROR_CODES.get(status),
            expected_version=expected_version,
            current_version=current_version,
        )
    )


async def _process_item(
    session: AsyncSession,
    context: RequestContext,
    *,
    operation_id: uuid.UUID,
    sequence: int,
    item: CategorizationPreviewItem | None,
    item_id: uuid.UUID,
    rule_set_version: int,
) -> ItemResult:
    """Run one item inside its own database transaction and commit its terminal result."""
    if item is None:
        # The preview was pruned between the claim and the resume; nothing can be proved.
        await _persist(
            session,
            operation_id=operation_id,
            item_id=item_id,
            sequence=sequence,
            transaction_id=None,
            status=executor.NOT_FOUND,
        )
        await session.commit()
        return ItemResult(
            item_id=item_id,
            transaction_id=None,
            status=executor.NOT_FOUND,
            error_code=executor.ERROR_CODES[executor.NOT_FOUND],
            expected_version=None,
            current_version=None,
        )

    ineligible = INELIGIBLE_PREVIEW_STATUS.get(item.status)
    if ineligible is not None:
        # Robust against a stale or malicious client asking to apply something the preview never
        # proposed: the recorded status is replayed, and no mutation is attempted.
        await _persist(
            session,
            operation_id=operation_id,
            item_id=item_id,
            sequence=sequence,
            transaction_id=item.transaction_id,
            status=ineligible,
        )
        await session.commit()
        return ItemResult(
            item_id=item_id,
            transaction_id=item.transaction_id,
            status=ineligible,
            error_code=executor.ERROR_CODES.get(ineligible),
            expected_version=item.transaction_version,
            current_version=None,
        )

    if (
        item.status != "matched"
        or item.rule_id is None
        or item.rule_version is None
        or item.category_id is None
        or item.transaction_version is None
    ):
        await _persist(
            session,
            operation_id=operation_id,
            item_id=item_id,
            sequence=sequence,
            transaction_id=item.transaction_id,
            status=executor.NO_MATCH,
        )
        await session.commit()
        return ItemResult(
            item_id=item_id,
            transaction_id=item.transaction_id,
            status=executor.NO_MATCH,
            error_code=executor.ERROR_CODES[executor.NO_MATCH],
            expected_version=None,
            current_version=None,
        )

    outcome = await executor.execute_apply(
        session,
        context,
        executor.ApplyExpectation(
            transaction_id=item.transaction_id,
            transaction_version=item.transaction_version,
            rule_id=item.rule_id,
            rule_version=item.rule_version,
            category_id=item.category_id,
            category_version=item.category_version,
            rule_set_version=rule_set_version,
        ),
        # The mutation and the terminal result must land in one commit, so the executor never
        # commits on its own here.
        commit=False,
    )
    current_version = outcome.current_version
    if outcome.applied and outcome.transaction is not None:
        current_version = outcome.transaction.version
    await _persist(
        session,
        operation_id=operation_id,
        item_id=item_id,
        sequence=sequence,
        transaction_id=item.transaction_id,
        status=outcome.status,
        expected_version=item.transaction_version,
        current_version=current_version,
    )
    await session.commit()
    return ItemResult(
        item_id=item_id,
        transaction_id=item.transaction_id,
        status=outcome.status,
        error_code=outcome.error_code,
        expected_version=item.transaction_version,
        current_version=current_version,
    )


async def _record_failure(
    session: AsyncSession,
    *,
    operation_id: uuid.UUID,
    item_id: uuid.UUID,
    sequence: int,
    transaction_id: uuid.UUID | None,
) -> ItemResult | None:
    """Persist a ``failed`` terminal result in a fresh transaction after an item was rolled back.

    If this write itself fails the item is simply left unattempted: a later retry with the same
    idempotency key will process it again, which is strictly safer than inventing a terminal state
    we could not durably record.
    """
    await session.rollback()
    try:
        await _persist(
            session,
            operation_id=operation_id,
            item_id=item_id,
            sequence=sequence,
            transaction_id=transaction_id,
            status=executor.FAILED,
        )
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("Could not persist a failed categorization apply result")
        return None
    return ItemResult(
        item_id=item_id,
        transaction_id=transaction_id,
        status=executor.FAILED,
        error_code=executor.ERROR_CODES[executor.FAILED],
        expected_version=None,
        current_version=None,
    )


async def apply_preview_items(
    session: AsyncSession,
    context: RequestContext,
    preview_id: uuid.UUID,
    data: CategorizationApplyRequest,
    idempotency_key: str,
) -> BulkApplyOutcome:
    workspace_id = context.workspace.id
    item_ids = list(data.item_ids)
    request_hash = canonical_request_hash(workspace_id, preview_id, item_ids)

    # An operation already claimed by this key is replayed or resumed without re-checking preview
    # expiry: a caller retrying an interrupted request must be able to recover its results even if
    # the preview's TTL has since elapsed.
    existing = await repository.find_operation(session, workspace_id, idempotency_key)
    if existing is not None:
        if existing.request_hash != request_hash or existing.preview_id != preview_id:
            raise _idempotency_conflict()
        preview = await preview_repository.get_preview(session, workspace_id, preview_id)
        operation = existing
    else:
        # A brand new operation requires a live, unexpired, workspace-owned preview. The shared row
        # lock is held through item validation and operation claim/commit. Cleanup takes FOR UPDATE
        # on the same row, establishing a strict winner without locking financial transactions.
        preview = await preview_service.get_preview(
            session,
            workspace_id,
            preview_id,
            for_share=True,
        )
        items = await repository.load_items(session, preview_id, item_ids)
        missing = [item for item in item_ids if item not in items]
        if missing:
            # Foreign and nonexistent identifiers are indistinguishable: the lookup is already
            # scoped to this workspace's preview, so nothing cross-workspace can be inferred.
            raise ApiError(
                status_code=422,
                code="CATEGORIZATION_PREVIEW_ITEM_NOT_FOUND",
                message="Every item must belong to this preview",
            )
        try:
            operation = await repository.claim_operation(
                session,
                workspace_id=workspace_id,
                preview_id=preview_id,
                actor_user_id=context.user.id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                requested_count=len(item_ids),
            )
            await session.commit()
        except IntegrityError:
            await session.rollback()
            claimed = await repository.find_operation(session, workspace_id, idempotency_key)
            if claimed is None:
                raise
            operation = claimed
        if operation.request_hash != request_hash or operation.preview_id != preview_id:
            # Lost the race to a concurrent request carrying the same key for something else.
            raise _idempotency_conflict()

    # Plain values, captured once: a rollback later in the loop expires the ORM instances, and
    # reading an expired attribute would attempt implicit IO outside the async context.
    operation_id = operation.id
    rule_set_version = preview.rule_set_version if preview is not None else None
    persisted = await repository.results_for(session, operation_id)
    items = await repository.load_items(session, preview_id, item_ids)

    results: list[ItemResult] = []
    for sequence, item_id in enumerate(item_ids):
        recorded = persisted.get(item_id)
        if recorded is not None:
            # Terminal results are replayed, never retried. This is what keeps an applied
            # transaction from being mutated twice and a conflict from being silently re-run.
            results.append(_from_row(recorded))
            continue
        if rule_set_version is None:
            # The preview is gone, so nothing about this item can be proved any more.
            results.append(
                await _process_item(
                    session,
                    context,
                    operation_id=operation_id,
                    sequence=sequence,
                    item=None,
                    item_id=item_id,
                    rule_set_version=0,
                )
            )
            continue
        try:
            results.append(
                await _process_item(
                    session,
                    context,
                    operation_id=operation_id,
                    sequence=sequence,
                    item=items.get(item_id),
                    item_id=item_id,
                    rule_set_version=rule_set_version,
                )
            )
        except IntegrityError:
            # A concurrent request holding the same key committed this item first. Its terminal
            # result is authoritative; re-read it rather than mutating anything again.
            await session.rollback()
            replay = await repository.results_for(session, operation_id)
            existing_result = replay.get(item_id)
            if existing_result is None:
                raise
            results.append(_from_row(existing_result))
        except ApiError:
            raise
        except Exception:
            # Unexpected per-item failure: isolate it, keep the correlation in the log, and never
            # surface the exception text to the caller.
            logger.exception(
                "Categorization apply item failed",
                extra={
                    "operation_id": str(operation_id),
                    "preview_item_id": str(item_id),
                    "request_id": context.request_id,
                },
            )
            item = items.get(item_id)
            failure = await _record_failure(
                session,
                operation_id=operation_id,
                item_id=item_id,
                sequence=sequence,
                transaction_id=item.transaction_id if item is not None else None,
            )
            if failure is None:
                raise
            results.append(failure)

    if len(results) == len(item_ids):
        await repository.complete_operation(session, operation_id, datetime.now(UTC))
        await session.commit()
    return BulkApplyOutcome(preview_id=preview_id, operation_id=operation_id, results=results)
