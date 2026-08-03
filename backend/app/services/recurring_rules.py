import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models.accounts import Account
from app.db.models.automations import RecurringRule, RecurringRuleExecution
from app.db.models.categories import Category
from app.db.models.transactions import FinancialTransaction
from app.db.models.users import User, Workspace
from app.dependencies.context import RequestContext
from app.schemas.automations import RecurringRuleCreate, RecurringRuleUpdate
from app.schemas.transactions import TransactionCreate
from app.services import automation_runs, recurrence, transactions
from app.services.audit import record_audit, snapshot


async def get_rule(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    rule_id: uuid.UUID,
    *,
    include_deleted: bool = False,
) -> RecurringRule:
    filters = [
        RecurringRule.id == rule_id,
        RecurringRule.workspace_id == workspace_id,
    ]
    if not include_deleted:
        filters.append(RecurringRule.deleted_at.is_(None))
    rule = await session.scalar(select(RecurringRule).where(*filters))
    if rule is None:
        raise ApiError(
            status_code=404,
            code="RECURRING_RULE_NOT_FOUND",
            message="Recurring rule was not found",
        )
    return rule


async def list_rules(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    include_deleted: bool,
    limit: int,
    offset: int,
) -> tuple[list[RecurringRule], int]:
    filters = [RecurringRule.workspace_id == workspace_id]
    if not include_deleted:
        filters.append(RecurringRule.deleted_at.is_(None))
    total = int(
        await session.scalar(select(func.count()).select_from(RecurringRule).where(*filters)) or 0
    )
    items = list(
        (
            await session.scalars(
                select(RecurringRule)
                .where(*filters)
                .order_by(RecurringRule.created_at.desc(), RecurringRule.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return items, total


async def create_rule(
    session: AsyncSession, context: RequestContext, data: RecurringRuleCreate
) -> RecurringRule:
    values = data.model_dump()
    values["name"] = data.name.strip()
    values["currency"] = data.currency.upper()
    values["schedule_rrule"] = recurrence.normalize_rrule(data.schedule_rrule)
    await _validate_financial_references(session, context.workspace.id, values)
    now = datetime.now(UTC)
    rule = RecurringRule(
        workspace_id=context.workspace.id,
        created_by=context.user.id,
        next_run_at=(
            recurrence.next_occurrence(
                values["schedule_rrule"], data.timezone, after=now, anchor=now
            )
            if data.is_active
            else None
        ),
        **values,
    )
    session.add(rule)
    await session.flush()
    await _audit(session, context, rule, "recurring.create", None)
    await session.commit()
    await session.refresh(rule)
    return rule


async def update_rule(
    session: AsyncSession,
    context: RequestContext,
    rule_id: uuid.UUID,
    data: RecurringRuleUpdate,
) -> RecurringRule:
    rule = await get_rule(session, context.workspace.id, rule_id)
    if rule.version != data.version:
        raise ApiError(status_code=409, code="VERSION_CONFLICT", message="Version is stale")
    before = snapshot("recurring_rule", rule)
    changes = data.model_dump(exclude_unset=True, exclude={"version"})
    current = {field: getattr(rule, field) for field in RecurringRuleCreate.model_fields}
    current.update(changes)
    merged = RecurringRuleCreate.model_validate(current)
    values = merged.model_dump()
    values["name"] = merged.name.strip()
    values["currency"] = merged.currency.upper()
    values["schedule_rrule"] = recurrence.normalize_rrule(merged.schedule_rrule)
    await _validate_financial_references(session, context.workspace.id, values)
    for field, value in values.items():
        setattr(rule, field, value)
    now = datetime.now(UTC)
    rule.next_run_at = (
        recurrence.next_occurrence(
            rule.schedule_rrule,
            rule.timezone,
            after=now,
            anchor=rule.created_at,
        )
        if rule.is_active
        else None
    )
    rule.version += 1
    rule.updated_at = now
    await _audit(session, context, rule, "recurring.update", before)
    await session.commit()
    await session.refresh(rule)
    return rule


async def change_state(
    session: AsyncSession,
    context: RequestContext,
    rule_id: uuid.UUID,
    action: str,
) -> RecurringRule:
    rule = await get_rule(
        session,
        context.workspace.id,
        rule_id,
        include_deleted=action == "restore",
    )
    before = snapshot("recurring_rule", rule)
    now = datetime.now(UTC)
    if action == "delete":
        rule.deleted_at = now
        rule.is_active = False
        rule.next_run_at = None
    elif action == "restore":
        rule.deleted_at = None
        rule.is_active = True
        rule.next_run_at = recurrence.next_occurrence(
            rule.schedule_rrule, rule.timezone, after=now, anchor=rule.created_at
        )
    elif action == "pause":
        rule.is_active = False
        rule.next_run_at = None
    elif action == "resume":
        rule.is_active = True
        rule.next_run_at = recurrence.next_occurrence(
            rule.schedule_rrule, rule.timezone, after=now, anchor=rule.created_at
        )
    rule.version += 1
    rule.updated_at = now
    audit_action = {
        "delete": "recurring.update",
        "restore": "recurring.update",
        "pause": "recurring.pause",
        "resume": "recurring.resume",
    }[action]
    await _audit(session, context, rule, audit_action, before)
    await session.commit()
    await session.refresh(rule)
    return rule


async def due_rules(
    session: AsyncSession,
    workspace_id: uuid.UUID | None,
    *,
    now: datetime,
    limit: int,
) -> list[RecurringRule]:
    filters = [
        RecurringRule.is_active.is_(True),
        RecurringRule.deleted_at.is_(None),
        RecurringRule.next_run_at.is_not(None),
        RecurringRule.next_run_at <= now,
    ]
    if workspace_id is not None:
        filters.append(RecurringRule.workspace_id == workspace_id)
    return list(
        (
            await session.scalars(
                select(RecurringRule)
                .where(*filters)
                .order_by(RecurringRule.next_run_at, RecurringRule.id)
                .limit(limit)
            )
        ).all()
    )


async def execute_rule(
    session: AsyncSession,
    rule: RecurringRule,
    *,
    scheduled_for: datetime,
    idempotency_key: str,
    service_account_id: uuid.UUID | None,
    initiated_by: uuid.UUID | None,
    request_id: str,
    trigger_type: str,
) -> tuple[RecurringRuleExecution, bool]:
    scheduled = scheduled_for.astimezone(UTC).replace(microsecond=0)
    run, duplicate = await automation_runs.begin_run(
        session,
        workspace_id=rule.workspace_id,
        automation_type="recurring_rule",
        trigger_type=trigger_type,
        idempotency_key=idempotency_key,
        service_account_id=service_account_id,
        initiated_by=initiated_by,
        request_id=request_id,
        input_summary={"rule_id": str(rule.id), "scheduled_for": scheduled.isoformat()},
    )
    existing = await session.scalar(
        select(RecurringRuleExecution).where(
            RecurringRuleExecution.rule_id == rule.id,
            RecurringRuleExecution.scheduled_for == scheduled,
        )
    )
    if existing is not None:
        return existing, True
    if duplicate:
        raise ApiError(
            status_code=409,
            code="RECURRING_RULE_ALREADY_EXECUTED",
            message="The automation run exists without a recurring execution result",
        )
    if service_account_id is not None:
        expected = (
            rule.next_run_at.astimezone(UTC).replace(microsecond=0)
            if rule.next_run_at is not None
            else None
        )
        if (
            expected is None
            or scheduled != expected
            or expected > datetime.now(UTC)
            or rule.deleted_at is not None
            or not rule.is_active
        ):
            raise ApiError(
                status_code=409,
                code="RECURRING_RULE_INVALID",
                message="Scheduled execution must match the currently due occurrence",
            )
    execution = RecurringRuleExecution(
        rule_id=rule.id,
        scheduled_for=scheduled,
        automation_run_id=run.id,
        status="created",
        created_at=datetime.now(UTC),
    )
    session.add(execution)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(
            select(RecurringRuleExecution).where(
                RecurringRuleExecution.rule_id == rule.id,
                RecurringRuleExecution.scheduled_for == scheduled,
            )
        )
        if existing is None:
            raise
        return existing, True
    if rule.deleted_at is not None or not rule.is_active:
        execution.status = "skipped"
        automation_runs.complete_run(run, {"status": "skipped"}, status="skipped")
    elif rule.creation_mode == "reminder_only":
        execution.status = "reminder_sent"
        automation_runs.complete_run(run, {"status": "reminder_sent"})
    else:
        transaction = await _create_transaction_from_rule(
            session, rule, scheduled, request_id=request_id
        )
        execution.transaction_id = transaction.id
        execution.status = "draft_created" if rule.creation_mode == "draft" else "confirmed_created"
        automation_runs.complete_run(
            run,
            {"status": execution.status, "transaction_id": str(transaction.id)},
        )
    now = datetime.now(UTC)
    execution.completed_at = now
    rule.last_run_at = scheduled
    rule.next_run_at = recurrence.next_occurrence(
        rule.schedule_rrule,
        rule.timezone,
        after=max(now, scheduled),
        anchor=rule.created_at,
    )
    rule.updated_at = now
    await record_audit(
        session,
        workspace_id=rule.workspace_id,
        actor_user_id=initiated_by,
        entity_type="recurring_rule",
        entity_id=rule.id,
        action="recurring.execute",
        before_data=None,
        after_data={
            "scheduled_for": scheduled.isoformat(),
            "status": execution.status,
            "transaction_id": str(execution.transaction_id) if execution.transaction_id else None,
        },
        request_id=request_id,
        source="automation" if service_account_id else "api",
    )
    await session.commit()
    await session.refresh(execution)
    return execution, False


async def list_history(
    session: AsyncSession,
    rule: RecurringRule,
    *,
    limit: int,
    offset: int,
) -> tuple[list[RecurringRuleExecution], int]:
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(RecurringRuleExecution)
            .where(RecurringRuleExecution.rule_id == rule.id)
        )
        or 0
    )
    items = list(
        (
            await session.scalars(
                select(RecurringRuleExecution)
                .where(RecurringRuleExecution.rule_id == rule.id)
                .order_by(RecurringRuleExecution.scheduled_for.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return items, total


async def _validate_financial_references(
    session: AsyncSession, workspace_id: uuid.UUID, values: dict[str, object]
) -> None:
    recurrence.timezone(str(values["timezone"]))
    if values["rule_type"] != values["transaction_type"]:
        raise _invalid("rule_type must match transaction_type")
    account = await session.scalar(
        select(Account).where(
            Account.id == values["account_id"],
            Account.workspace_id == workspace_id,
            Account.deleted_at.is_(None),
            Account.is_archived.is_(False),
        )
    )
    if account is None:
        raise _invalid("Active source account was not found")
    if account.currency != values["currency"]:
        raise _invalid("Rule currency must match account currency")
    transaction_type = str(values["transaction_type"])
    target_id = values.get("target_account_id")
    category_id = values.get("category_id")
    if transaction_type == "transfer":
        if target_id is None or target_id == account.id or category_id is not None:
            raise _invalid("Transfer rule requires a different target account and no category")
        target = await session.scalar(
            select(Account).where(
                Account.id == target_id,
                Account.workspace_id == workspace_id,
                Account.deleted_at.is_(None),
                Account.is_archived.is_(False),
            )
        )
        if target is None or target.currency != account.currency:
            raise _invalid("Transfer accounts must be active and use the same currency")
    elif target_id is not None:
        raise _invalid("Target account is supported only for transfer rules")
    if transaction_type in {"income", "expense"}:
        if category_id is None:
            raise _invalid("Income and expense rules require a category")
        category = await session.scalar(
            select(Category).where(
                Category.id == category_id,
                Category.workspace_id == workspace_id,
                Category.deleted_at.is_(None),
                Category.is_archived.is_(False),
            )
        )
        expected = {transaction_type, "both"}
        if category is None or category.category_type not in expected:
            raise _invalid("Active category type does not match the recurring rule")


async def _create_transaction_from_rule(
    session: AsyncSession,
    rule: RecurringRule,
    scheduled_for: datetime,
    *,
    request_id: str,
) -> FinancialTransaction:
    user = await session.get(User, rule.created_by)
    workspace = await session.get(Workspace, rule.workspace_id)
    if user is None or workspace is None:
        raise ApiError(
            status_code=409,
            code="RECURRING_RULE_INVALID",
            message="Recurring rule owner or workspace no longer exists",
        )
    context = RequestContext(user=user, workspace=workspace, role="owner", request_id=request_id)
    return await transactions.create_transaction(
        session,
        context,
        TransactionCreate(
            occurred_at=scheduled_for,
            transaction_type=rule.transaction_type,  # type: ignore[arg-type]
            amount=rule.amount,
            currency=rule.currency,
            account_id=rule.account_id,
            target_account_id=rule.target_account_id,
            category_id=rule.category_id,
            counterparty=rule.counterparty,
            description=rule.description,
            comment=rule.comment,
            status="draft" if rule.creation_mode == "draft" else "confirmed",
            source="automation",
            external_id=f"recurring:{rule.id}:{scheduled_for.isoformat()}",
        ),
        commit=False,
        audit_source="automation",
    )


async def _audit(
    session: AsyncSession,
    context: RequestContext,
    rule: RecurringRule,
    action: str,
    before: dict[str, object] | None,
) -> None:
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="recurring_rule",
        entity_id=rule.id,
        action=action,
        before_data=before,
        after_data=snapshot("recurring_rule", rule),
        request_id=context.request_id,
    )


def _invalid(message: str) -> ApiError:
    return ApiError(status_code=422, code="RECURRING_RULE_INVALID", message=message)
