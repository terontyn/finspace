import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models.categories import Category
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
from app.schemas.transactions import TransactionUpdate
from app.services import payees as payee_service
from app.services import transactions as transaction_service
from app.services.audit import record_audit, snapshot

_UNICODE_WHITESPACE = re.compile(r"\s+", flags=re.UNICODE)


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


def normalize_match_text(value: str | None) -> str:
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    normalized = _UNICODE_WHITESPACE.sub(" ", normalized.strip())
    return unicodedata.normalize("NFKC", normalized.casefold())


def _not_found() -> ApiError:
    return ApiError(
        status_code=404,
        code="CATEGORIZATION_RULE_NOT_FOUND",
        message="Categorization rule was not found",
    )


def _check_version(rule: CategorizationRule, version: int) -> None:
    if rule.version != version:
        raise ApiError(status_code=409, code="VERSION_CONFLICT", message="Version is stale")


def _category_compatible(transaction_type: str, category_type: str) -> bool:
    if transaction_type == "income":
        return category_type in {"income", "both"}
    if transaction_type == "expense":
        return category_type in {"expense", "both"}
    return transaction_type != "transfer"


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


async def create_rule(
    session: AsyncSession,
    context: RequestContext,
    data: CategorizationRuleCreate,
) -> CategorizationRule:
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
    await session.commit()
    await session.refresh(rule)
    return rule


async def update_rule(
    session: AsyncSession,
    context: RequestContext,
    rule_id: uuid.UUID,
    data: CategorizationRuleUpdate,
) -> CategorizationRule:
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
    await session.commit()
    await session.refresh(rule)
    return rule


async def delete_rule(
    session: AsyncSession,
    context: RequestContext,
    rule_id: uuid.UUID,
    version: int,
) -> CategorizationRule:
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
    await session.commit()
    await session.refresh(rule)
    return rule


async def restore_rule(
    session: AsyncSession,
    context: RequestContext,
    rule_id: uuid.UUID,
    version: int,
) -> CategorizationRule:
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
    await session.commit()
    await session.refresh(rule)
    return rule


def _matches(rule: CategorizationRule, transaction: FinancialTransaction) -> bool:
    if transaction.transaction_type == "transfer":
        return False
    if rule.transaction_type is not None and rule.transaction_type != transaction.transaction_type:
        return False
    if rule.account_id is not None and rule.account_id != transaction.account_id:
        return False
    if rule.payee_id is not None and rule.payee_id != transaction.payee_id:
        return False
    if rule.counterparty_contains is not None:
        needle = normalize_match_text(rule.counterparty_contains)
        if needle not in normalize_match_text(transaction.counterparty):
            return False
    if rule.description_contains is not None:
        needle = normalize_match_text(rule.description_contains)
        if needle not in normalize_match_text(transaction.description):
            return False
    return True


async def match_transaction(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    transaction: FinancialTransaction,
) -> CategorizationMatch | None:
    for rule in await repository.active_rules(session, workspace_id):
        if not _matches(rule, transaction):
            continue
        category = await category_repository.get_category(session, workspace_id, rule.category_id)
        if category is None or category.is_archived:
            continue
        if not _category_compatible(transaction.transaction_type, category.category_type):
            continue
        return CategorizationMatch(rule=rule, category=category)
    return None


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


async def _lock_current_match(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    transaction: FinancialTransaction,
    match: CategorizationMatch,
) -> None:
    matched_version = match.rule.version
    matched_category_id = match.category.id
    current_rule = await repository.get_rule(
        session,
        workspace_id,
        match.rule.id,
        include_deleted=True,
        for_update=True,
    )
    if (
        current_rule is None
        or current_rule.version != matched_version
        or current_rule.deleted_at is not None
        or not current_rule.is_active
        or current_rule.category_id != matched_category_id
        or not _matches(current_rule, transaction)
    ):
        raise _rule_changed_during_apply()


async def apply_to_transaction(
    session: AsyncSession,
    context: RequestContext,
    transaction_id: uuid.UUID,
    version: int,
) -> CategorizationApplyResult:
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
    await _lock_current_match(
        session,
        context.workspace.id,
        transaction,
        match,
    )
    updated = await transaction_service.update_transaction(
        session,
        context,
        transaction.id,
        TransactionUpdate(version=version, category_id=match.category.id),
        audit_source="api",
    )
    return CategorizationApplyResult(
        transaction=updated,
        match=match,
        applied=True,
        reason="applied",
    )
