"""The one safe path that writes a categorization decision to a transaction.

Both the existing single-transaction apply and the persisted bulk apply run through
``execute_apply``. There is deliberately no bulk-only mutation path: a second implementation would
be a second set of locking and revalidation semantics to keep in sync.

A persisted preview is evidence of what was proposed earlier; it is never authorization to write.
Every expectation carried in :class:`ApplyExpectation` is re-proved live, under locks, inside the
caller's transaction, immediately before the mutation.

Lock order (identical for every caller, never reversed):

1. ``CategorizationRuleSetControl`` ``FOR SHARE`` — the Stage A1 gate; rule mutations take it
   ``FOR UPDATE`` and therefore wait, while concurrent applies stay compatible.
2. ``MonthCloseControl`` ``FOR UPDATE``
3. ``FinancialTransaction`` ``FOR UPDATE``
4. proposed ``CategorizationRule`` ``FOR SHARE`` — pins the row the proposal names.
5. target ``Category`` ``FOR SHARE`` — closes the concurrency gap deferred from A1/A2.
6. deterministic re-match under the locks held above.
7. ``transactions.update_transaction`` — re-acquires (2) and (3), already held here.

Steps 2 and 3 are taken in exactly the order ``transactions.update_transaction`` already uses, and
deliberately *before* the rule and category locks. Taking them later would invert that order against
every other transaction mutation in the codebase and open a real deadlock window.

Rule and category locks come last and cannot deadlock in that position: rule mutations must pass the
exclusive rule-set gate at step 1 before they can touch a rule, and category mutations in
``services.categories`` take no row lock at all (they are purely optimistic on ``version``), so
neither can hold one of those rows while waiting for a transaction or month-close lock.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models.categories import Category
from app.db.models.transactions import FinancialTransaction
from app.dependencies.context import RequestContext
from app.repositories import categorization_rules as rule_repository
from app.repositories import transactions as transaction_repository
from app.schemas.transactions import TransactionUpdate
from app.services import transactions as transaction_service
from app.services.categorization_matcher import category_compatible, prepare_rule_set
from app.services.financial_period_guard import get_or_create_control

# Terminal outcomes shared by the executor and the persisted bulk results.
APPLIED = "applied"
TRANSACTION_CHANGED = "transaction_changed"
RULE_CHANGED = "rule_changed"
CATEGORY_CHANGED = "category_changed"
ALREADY_CATEGORIZED = "already_categorized"
SPLIT = "split"
TRANSFER = "transfer"
RECONCILED = "reconciled"
CLOSED_PERIOD = "closed_period"
NO_MATCH = "no_match"
NOT_FOUND = "not_found"
FAILED = "failed"

ERROR_CODES: dict[str, str] = {
    TRANSACTION_CHANGED: "CATEGORIZATION_TRANSACTION_CHANGED",
    RULE_CHANGED: "CATEGORIZATION_RULE_CHANGED",
    CATEGORY_CHANGED: "CATEGORIZATION_CATEGORY_CHANGED",
    ALREADY_CATEGORIZED: "CATEGORIZATION_ALREADY_CATEGORIZED",
    SPLIT: "CATEGORIZATION_TRANSACTION_SPLIT",
    TRANSFER: "CATEGORIZATION_TRANSFER_NOT_ELIGIBLE",
    RECONCILED: "RECONCILED_TRANSACTION_IMMUTABLE",
    CLOSED_PERIOD: "MONTH_CLOSED",
    NO_MATCH: "CATEGORIZATION_NO_MATCH",
    NOT_FOUND: "TRANSACTION_NOT_FOUND",
    FAILED: "CATEGORIZATION_APPLY_FAILED",
}

# Guards are evaluated in this order, so a transaction that violates several invariants reports the
# most specific reason. Identity checks come first: if the caller's evidence is stale, nothing else
# it claims can be trusted.
GUARD_PRECEDENCE = (
    NOT_FOUND,
    TRANSACTION_CHANGED,
    TRANSFER,
    ALREADY_CATEGORIZED,
    SPLIT,
    RECONCILED,
    RULE_CHANGED,
    CATEGORY_CHANGED,
    CLOSED_PERIOD,
)


@dataclass(frozen=True)
class ApplyExpectation:
    """Everything the caller claims was true when the proposal was produced.

    ``rule_set_version`` and ``category_version`` are optional because the single-transaction
    endpoint derives its proposal from a live preview computed microseconds earlier and has never
    carried those expectations; bulk apply always supplies both.
    """

    transaction_id: uuid.UUID
    transaction_version: int
    rule_id: uuid.UUID
    rule_version: int
    category_id: uuid.UUID
    category_version: int | None = None
    rule_set_version: int | None = None


@dataclass(frozen=True)
class ApplyOutcome:
    status: str
    error_code: str | None = None
    transaction: FinancialTransaction | None = None
    expected_version: int | None = None
    current_version: int | None = None

    @property
    def applied(self) -> bool:
        return self.status == APPLIED


def _outcome(
    status: str,
    *,
    transaction: FinancialTransaction | None = None,
    expected_version: int | None = None,
    current_version: int | None = None,
) -> ApplyOutcome:
    return ApplyOutcome(
        status=status,
        error_code=ERROR_CODES.get(status),
        transaction=transaction,
        expected_version=expected_version,
        current_version=current_version,
    )


async def _lock_category(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    category_id: uuid.UUID,
) -> Category | None:
    statement = (
        select(Category)
        .where(Category.id == category_id, Category.workspace_id == workspace_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    return await session.scalar(statement)


async def execute_apply(
    session: AsyncSession,
    context: RequestContext,
    expectation: ApplyExpectation,
    *,
    commit: bool,
) -> ApplyOutcome:
    """Re-prove a proposal under locks and, only if everything still holds, apply it.

    Returns a terminal outcome instead of raising for expected business and concurrency conditions.
    Unexpected failures are left to propagate so the caller can isolate and classify them.
    """
    workspace_id = context.workspace.id

    # 1. Rule-set gate. Held until the caller's transaction commits.
    control = await rule_repository.get_or_create_rule_set_control(
        session,
        workspace_id,
        for_share=True,
    )
    rule_set_changed = (
        expectation.rule_set_version is not None and control.version != expectation.rule_set_version
    )

    # 2/3. Month-close control then transaction, matching the order update_transaction uses. Taking
    # them here rather than after the rule and category locks keeps a single global order.
    await get_or_create_control(session, workspace_id, for_update=True)
    transaction = await transaction_repository.get_transaction(
        session,
        workspace_id,
        expectation.transaction_id,
        for_update=True,
    )
    if transaction is None:
        return _outcome(NOT_FOUND)
    if transaction.version != expectation.transaction_version:
        return _outcome(
            TRANSACTION_CHANGED,
            transaction=transaction,
            expected_version=expectation.transaction_version,
            current_version=transaction.version,
        )

    # State guards, most specific first. These are checked before the rule-set comparison so a
    # transaction that can never be categorized reports why, rather than blaming the rule set.
    if transaction.transaction_type == "transfer":
        return _outcome(TRANSFER, transaction=transaction)
    if transaction.category_id is not None:
        return _outcome(ALREADY_CATEGORIZED, transaction=transaction)
    if await transaction_repository.get_splits(session, transaction.id):
        return _outcome(SPLIT, transaction=transaction)
    if transaction.status == "reconciled":
        return _outcome(RECONCILED, transaction=transaction)

    if rule_set_changed:
        # Conservative by design: A3 never re-matches under a moved rule set, it demands a new
        # preview.
        return _outcome(RULE_CHANGED, transaction=transaction)

    # 4. Pin the proposed rule and re-prove it is still the deterministic first valid match.
    proposed_rule = await rule_repository.get_rule(
        session,
        workspace_id,
        expectation.rule_id,
        include_deleted=True,
        for_share=True,
    )
    if (
        proposed_rule is None
        or proposed_rule.version != expectation.rule_version
        or proposed_rule.deleted_at is not None
        or not proposed_rule.is_active
        or proposed_rule.category_id != expectation.category_id
    ):
        return _outcome(RULE_CHANGED, transaction=transaction)

    # 5. Live target category, locked, in this same transaction. This is proved *before* the
    # deterministic re-match on purpose: an archived, retyped or re-versioned target category also
    # removes its rule from the matcher, and "category_changed" is the specific, actionable reason a
    # caller needs — "no_match" would hide why the proposal became invalid.
    category = await _lock_category(session, workspace_id, expectation.category_id)
    if (
        category is None
        or category.deleted_at is not None
        or category.is_archived
        or not category_compatible(transaction.transaction_type, category.category_type)
        or (
            expectation.category_version is not None
            and category.version != expectation.category_version
        )
    ):
        return _outcome(CATEGORY_CHANGED, transaction=transaction)

    # 6. The proposal must still be the deterministic first valid match.
    rule_set = await prepare_rule_set(session, workspace_id, refresh=True)
    confirmed = rule_set.match_transaction(transaction)
    if confirmed is None:
        return _outcome(NO_MATCH, transaction=transaction)
    if (
        confirmed.rule.id != expectation.rule_id
        or confirmed.rule.version != expectation.rule_version
        or confirmed.category.id != expectation.category_id
    ):
        return _outcome(RULE_CHANGED, transaction=transaction)

    # 7. The authoritative mutation path: month-close guard, transaction lock, version bump,
    # audit and sync outbox all stay owned by transactions.update_transaction.
    try:
        updated = await transaction_service.update_transaction(
            session,
            context,
            transaction.id,
            TransactionUpdate(
                version=expectation.transaction_version,
                category_id=expectation.category_id,
            ),
            commit=commit,
            audit_source="api",
        )
    except ApiError as error:
        if error.code == "MONTH_CLOSED":
            return _outcome(CLOSED_PERIOD, transaction=transaction)
        if error.code == "RECONCILED_TRANSACTION_IMMUTABLE":
            return _outcome(RECONCILED, transaction=transaction)
        if error.code == "VERSION_CONFLICT":
            return _outcome(
                TRANSACTION_CHANGED,
                transaction=transaction,
                expected_version=expectation.transaction_version,
                current_version=transaction.version,
            )
        raise
    return ApplyOutcome(
        status=APPLIED,
        transaction=updated,
        expected_version=expectation.transaction_version,
        current_version=updated.version,
    )
