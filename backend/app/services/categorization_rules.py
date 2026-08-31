import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models.categories import Category
from app.db.models.categorization_rule_sets import CategorizationRuleSetControl
from app.db.models.categorization_rules import CategorizationRule
from app.db.models.transactions import FinancialTransaction
from app.dependencies.context import RequestContext
from app.repositories import accounts as account_repository
from app.repositories import categories as category_repository
from app.repositories import categorization_rules as repository
from app.repositories import transactions as transaction_repository
from app.schemas.categorization_rules import (
    CategorizationApplyReason,
    CategorizationRuleCreate,
    CategorizationRuleUpdate,
)
from app.services import categorization_executor as executor
from app.services import payees as payee_service
from app.services.audit import record_audit, snapshot
from app.services.categorization_matcher import (
    MatchCandidate,
    category_compatible,
    normalize_match_text,
    prepare_rule_set,
)

# Fields that can change which rule matches first, or in what order rules are evaluated. ``name``
# is deliberately absent: renaming a rule cannot affect matching or ordering, so it does not
# advance the rule-set revision even though it still advances the rule's own version and writes an
# audit entry.
RULE_SET_SEMANTIC_FIELDS = (
    "priority",
    "is_active",
    "transaction_type",
    "account_id",
    "payee_id",
    "counterparty_contains",
    "description_contains",
    "category_id",
)


@dataclass(frozen=True)
class CategorizationMatch:
    rule: CategorizationRule
    category: Category


@dataclass(frozen=True)
class CategorizationApplyResult:
    transaction: FinancialTransaction
    match: CategorizationMatch | None
    applied: bool
    reason: CategorizationApplyReason


def _not_found() -> ApiError:
    return ApiError(
        status_code=404,
        code="CATEGORIZATION_RULE_NOT_FOUND",
        message="Categorization rule was not found",
    )


def _check_version(rule: CategorizationRule, version: int) -> None:
    if rule.version != version:
        raise ApiError(status_code=409, code="VERSION_CONFLICT", message="Version is stale")


_category_compatible = category_compatible


def _rule_changed_during_apply() -> ApiError:
    return ApiError(
        status_code=409,
        code="CATEGORIZATION_RULE_CHANGED",
        message="Categorization rule changed while it was being applied",
    )


def _has_matcher(values: dict[str, object]) -> bool:
    return any(
        values.get(field) is not None
        for field in (
            "transaction_type",
            "account_id",
            "payee_id",
            "counterparty_contains",
            "description_contains",
        )
    )


async def _validate_references(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    transaction_type: str | None,
    account_id: uuid.UUID | None,
    payee_id: uuid.UUID | None,
    category_id: uuid.UUID,
) -> Category:
    category = await category_repository.get_category(session, workspace_id, category_id)
    if category is None or category.is_archived:
        raise ApiError(
            status_code=404,
            code="CATEGORY_NOT_FOUND",
            message="Category was not found",
        )
    if account_id is not None:
        account = await account_repository.get_account(session, workspace_id, account_id)
        if account is None or account.is_archived:
            raise ApiError(
                status_code=404,
                code="ACCOUNT_NOT_FOUND",
                message="Account was not found",
            )
    if payee_id is not None:
        await payee_service.get_assignable_payee_for_write(session, workspace_id, payee_id)
    if transaction_type is not None and not _category_compatible(
        transaction_type, category.category_type
    ):
        raise ApiError(
            status_code=422,
            code="INVALID_CATEGORY_TYPE",
            message="Category type does not match transaction type",
        )
    return category


async def get_rule(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    rule_id: uuid.UUID,
    *,
    include_deleted: bool = False,
) -> CategorizationRule:
    rule = await repository.get_rule(
        session,
        workspace_id,
        rule_id,
        include_deleted=include_deleted,
    )
    if rule is None:
        raise _not_found()
    return rule


async def lock_rule_set_for_mutation(
    session: AsyncSession,
    workspace_id: uuid.UUID,
) -> CategorizationRuleSetControl:
    """Acquire the exclusive rule-set gate.

    Mandatory lock order is rule-set control -> categorization rule -> financial-period/transaction
    locks. Every semantic rule mutation calls this first, which is also what makes rule mutations
    wait for in-flight categorization applies holding the shared lock.
    """
    return await repository.get_or_create_rule_set_control(session, workspace_id, for_update=True)


def bump_rule_set(control: CategorizationRuleSetControl) -> int:
    """Advance the rule-set revision exactly once for a mutation that can affect matching."""
    control.version += 1
    control.updated_at = datetime.now(UTC)
    return control.version


def _rule_set_semantics(rule: CategorizationRule) -> tuple[object, ...]:
    return (*(getattr(rule, field) for field in RULE_SET_SEMANTIC_FIELDS), rule.deleted_at)


async def create_rule(
    session: AsyncSession,
    context: RequestContext,
    data: CategorizationRuleCreate,
) -> CategorizationRule:
    control = await lock_rule_set_for_mutation(session, context.workspace.id)
    await _validate_references(
        session,
        context.workspace.id,
        transaction_type=data.transaction_type,
        account_id=data.account_id,
        payee_id=data.payee_id,
        category_id=data.category_id,
    )
    rule = CategorizationRule(
        workspace_id=context.workspace.id,
        name=data.name,
        priority=data.priority,
        is_active=data.is_active,
        transaction_type=data.transaction_type,
        account_id=data.account_id,
        payee_id=data.payee_id,
        counterparty_contains=data.counterparty_contains,
        description_contains=data.description_contains,
        category_id=data.category_id,
        created_by=context.user.id,
        updated_by=context.user.id,
    )
    session.add(rule)
    await session.flush()
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="categorization_rule",
        entity_id=rule.id,
        action="create",
        before_data=None,
        after_data=snapshot("categorization_rule", rule),
        request_id=context.request_id,
    )
    # A new rule always joins the deterministic set, so the revision always advances.
    bump_rule_set(control)
    await session.commit()
    await session.refresh(rule)
    return rule


async def update_rule(
    session: AsyncSession,
    context: RequestContext,
    rule_id: uuid.UUID,
    data: CategorizationRuleUpdate,
) -> CategorizationRule:
    control = await lock_rule_set_for_mutation(session, context.workspace.id)
    rule = await repository.get_rule(
        session,
        context.workspace.id,
        rule_id,
        for_update=True,
    )
    if rule is None:
        raise _not_found()
    _check_version(rule, data.version)
    changes = data.model_dump(exclude_unset=True, exclude={"version"})
    for field in ("name", "priority", "is_active", "category_id"):
        if field in changes and changes[field] is None:
            raise ApiError(
                status_code=422,
                code="VALIDATION_ERROR",
                message=f"{field} cannot be null",
            )
    merged: dict[str, object] = {
        "name": rule.name,
        "priority": rule.priority,
        "is_active": rule.is_active,
        "transaction_type": rule.transaction_type,
        "account_id": rule.account_id,
        "payee_id": rule.payee_id,
        "counterparty_contains": rule.counterparty_contains,
        "description_contains": rule.description_contains,
        "category_id": rule.category_id,
    }
    merged.update(changes)
    if not _has_matcher(merged):
        raise ApiError(
            status_code=422,
            code="CATEGORIZATION_MATCHER_REQUIRED",
            message="At least one categorization rule matcher is required",
        )
    category_id = merged["category_id"]
    if not isinstance(category_id, uuid.UUID):
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="category_id is invalid",
        )
    await _validate_references(
        session,
        context.workspace.id,
        transaction_type=(
            str(merged["transaction_type"]) if merged["transaction_type"] is not None else None
        ),
        account_id=(merged["account_id"] if isinstance(merged["account_id"], uuid.UUID) else None),
        payee_id=(merged["payee_id"] if isinstance(merged["payee_id"], uuid.UUID) else None),
        category_id=category_id,
    )
    before = snapshot("categorization_rule", rule)
    semantics_before = _rule_set_semantics(rule)
    for field, value in changes.items():
        setattr(rule, field, value)
    rule.updated_by = context.user.id
    rule.updated_at = datetime.now(UTC)
    rule.version += 1
    await session.flush()
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="categorization_rule",
        entity_id=rule.id,
        action="update",
        before_data=before,
        after_data=snapshot("categorization_rule", rule),
        request_id=context.request_id,
    )
    # The rule's own version and audit entry follow the existing v0.11 contract for every accepted
    # PATCH, but the rule-set revision only advances when matching or ordering can actually change.
    if _rule_set_semantics(rule) != semantics_before:
        bump_rule_set(control)
    await session.commit()
    await session.refresh(rule)
    return rule


async def delete_rule(
    session: AsyncSession,
    context: RequestContext,
    rule_id: uuid.UUID,
    version: int,
) -> CategorizationRule:
    control = await lock_rule_set_for_mutation(session, context.workspace.id)
    rule = await repository.get_rule(
        session,
        context.workspace.id,
        rule_id,
        for_update=True,
    )
    if rule is None:
        raise _not_found()
    _check_version(rule, version)
    before = snapshot("categorization_rule", rule)
    now = datetime.now(UTC)
    rule.deleted_at = now
    rule.updated_at = now
    rule.updated_by = context.user.id
    rule.version += 1
    await session.flush()
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="categorization_rule",
        entity_id=rule.id,
        action="delete",
        before_data=before,
        after_data=snapshot("categorization_rule", rule),
        request_id=context.request_id,
    )
    # Archiving always removes the rule from the deterministic set. An already-archived rule cannot
    # reach this point because the lookup excludes soft-deleted rows and raises 404 instead.
    bump_rule_set(control)
    await session.commit()
    await session.refresh(rule)
    return rule


async def restore_rule(
    session: AsyncSession,
    context: RequestContext,
    rule_id: uuid.UUID,
    version: int,
) -> CategorizationRule:
    control = await lock_rule_set_for_mutation(session, context.workspace.id)
    rule = await repository.get_rule(
        session,
        context.workspace.id,
        rule_id,
        include_deleted=True,
        for_update=True,
    )
    if rule is None:
        raise _not_found()
    _check_version(rule, version)
    if rule.deleted_at is None:
        # Restoring an already-current rule is a no-op: no audit entry, no rule version bump and
        # no rule-set revision bump.
        return rule
    await _validate_references(
        session,
        context.workspace.id,
        transaction_type=rule.transaction_type,
        account_id=rule.account_id,
        payee_id=rule.payee_id,
        category_id=rule.category_id,
    )
    before = snapshot("categorization_rule", rule)
    now = datetime.now(UTC)
    rule.deleted_at = None
    rule.updated_at = now
    rule.updated_by = context.user.id
    rule.version += 1
    await session.flush()
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="categorization_rule",
        entity_id=rule.id,
        action="restore",
        before_data=before,
        after_data=snapshot("categorization_rule", rule),
        request_id=context.request_id,
    )
    # A restored rule rejoins the deterministic set.
    bump_rule_set(control)
    await session.commit()
    await session.refresh(rule)
    return rule


def _matches(rule: CategorizationRule, transaction: FinancialTransaction) -> bool:
    """Matcher-only check for one rule, used by the selected-rule re-proof during apply."""
    candidate = MatchCandidate.from_transaction(transaction)
    if candidate.transaction_type == "transfer":
        return False
    if rule.transaction_type is not None and rule.transaction_type != candidate.transaction_type:
        return False
    if rule.account_id is not None and rule.account_id != candidate.account_id:
        return False
    if rule.payee_id is not None and rule.payee_id != candidate.payee_id:
        return False
    if rule.counterparty_contains is not None:
        if normalize_match_text(rule.counterparty_contains) not in candidate.counterparty:
            return False
    if rule.description_contains is not None:
        if normalize_match_text(rule.description_contains) not in candidate.description:
            return False
    return True


async def match_transaction(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    transaction: FinancialTransaction,
    *,
    refresh: bool = False,
) -> CategorizationMatch | None:
    """Deterministic first valid match for one transaction.

    Backed by the shared prepared matcher, so this is two bounded queries rather than one active
    rule query plus a category lookup per candidate rule.
    """
    rule_set = await prepare_rule_set(session, workspace_id, refresh=refresh)
    match = rule_set.match_transaction(transaction)
    if match is None:
        return None
    return CategorizationMatch(rule=match.rule, category=match.category)


async def preview_transaction(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    transaction_id: uuid.UUID,
) -> tuple[FinancialTransaction, CategorizationMatch | None]:
    transaction = await transaction_repository.get_transaction(
        session,
        workspace_id,
        transaction_id,
    )
    if transaction is None:
        raise ApiError(
            status_code=404,
            code="TRANSACTION_NOT_FOUND",
            message="Transaction was not found",
        )
    return transaction, await match_transaction(session, workspace_id, transaction)


async def apply_to_transaction(
    session: AsyncSession,
    context: RequestContext,
    transaction_id: uuid.UUID,
    version: int,
) -> CategorizationApplyResult:
    """Single-transaction apply.

    The optimistic preview, the version check and the public reason vocabulary are unchanged; the
    guarded mutation itself now runs through the shared executor so single and bulk cannot drift
    apart in their locking or revalidation semantics. No persisted preview is required.
    """
    transaction, match = await preview_transaction(
        session,
        context.workspace.id,
        transaction_id,
    )
    if transaction.version != version:
        raise ApiError(status_code=409, code="VERSION_CONFLICT", message="Version is stale")
    if match is None:
        return CategorizationApplyResult(
            transaction=transaction,
            match=None,
            applied=False,
            reason="no_match",
        )
    splits = await transaction_repository.get_splits(session, transaction.id)
    if transaction.category_id is not None or splits:
        return CategorizationApplyResult(
            transaction=transaction,
            match=match,
            applied=False,
            reason="already_categorized",
        )
    outcome = await executor.execute_apply(
        session,
        context,
        executor.ApplyExpectation(
            transaction_id=transaction.id,
            transaction_version=version,
            rule_id=match.rule.id,
            rule_version=match.rule.version,
            category_id=match.category.id,
        ),
        commit=True,
    )
    if outcome.status == executor.APPLIED and outcome.transaction is not None:
        return CategorizationApplyResult(
            transaction=outcome.transaction,
            match=match,
            applied=True,
            reason="applied",
        )
    # Map the executor vocabulary back onto the established single-apply contract.
    if outcome.status == executor.NO_MATCH:
        return CategorizationApplyResult(
            transaction=outcome.transaction or transaction,
            match=None,
            applied=False,
            reason="no_match",
        )
    if outcome.status in {executor.ALREADY_CATEGORIZED, executor.SPLIT}:
        return CategorizationApplyResult(
            transaction=outcome.transaction or transaction,
            match=match,
            applied=False,
            reason="already_categorized",
        )
    if outcome.status == executor.TRANSACTION_CHANGED:
        raise ApiError(status_code=409, code="VERSION_CONFLICT", message="Version is stale")
    if outcome.status == executor.RECONCILED:
        raise ApiError(
            status_code=409,
            code="RECONCILED_TRANSACTION_IMMUTABLE",
            message="A reconciled transaction cannot be changed",
        )
    if outcome.status == executor.NOT_FOUND:
        raise ApiError(
            status_code=404,
            code="TRANSACTION_NOT_FOUND",
            message="Transaction was not found",
        )
    # rule_changed, category_changed and transfer all mean the proposal is no longer the
    # deterministic decision, which single apply has always reported as a rule change.
    raise _rule_changed_during_apply()
