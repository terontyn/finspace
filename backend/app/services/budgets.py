import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal, cast

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models.automations import MonthCloseControl, MonthCloseRevision, MonthClosure
from app.db.models.budgets import BudgetAllocation, BudgetPeriod, BudgetPlanRevision
from app.db.models.categories import Category
from app.db.models.users import Workspace
from app.dependencies.context import RequestContext
from app.repositories import budgets as repository
from app.schemas.budgets import (
    BudgetAllocationInput,
    BudgetAllocationProjection,
    BudgetCopyRequest,
    BudgetGroupResponse,
    BudgetMonthResponse,
    BudgetPlanRevisionResponse,
    BudgetRolloverResponse,
    BudgetUpsertRequest,
    RolloverPolicy,
)
from app.services.audit import record_audit, request_uuid
from app.services.budget_actuals import BudgetActual, money, project_budget_actuals
from app.services.financial_period_guard import (
    get_or_create_control,
    previous_month,
)
from app.services.month_close_fingerprint import canonical_decimal, hash_canonical

ZERO = Decimal("0.0000")


def parse_period(value: str) -> date:
    try:
        parsed = datetime.strptime(value, "%Y-%m").date()
    except ValueError as exc:
        raise ApiError(
            status_code=422,
            code="BUDGET_PERIOD_INVALID",
            message="Budget period must use YYYY-MM",
        ) from exc
    if parsed.year < 2000 or parsed.year > 2200 or parsed.strftime("%Y-%m") != value:
        raise ApiError(
            status_code=422,
            code="BUDGET_PERIOD_INVALID",
            message="Budget period is invalid",
        )
    return parsed.replace(day=1)


def _idempotency_key(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 255:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="X-Idempotency-Key is required and must contain at most 255 characters",
        )
    return normalized


def _semantic_payload(
    action: str,
    workspace_id: uuid.UUID,
    period: date,
    currency: str,
    payload: dict[str, object],
) -> dict[str, object]:
    return {
        "action": action,
        "workspace_id": str(workspace_id),
        "period": period.strftime("%Y-%m"),
        "currency": currency,
        "payload": payload,
    }


def _request_hash(
    action: str,
    workspace_id: uuid.UUID,
    period: date,
    currency: str,
    payload: dict[str, object],
) -> str:
    return hash_canonical(_semantic_payload(action, workspace_id, period, currency, payload))


def _canonical_allocations(items: list[BudgetAllocationInput]) -> list[dict[str, object]]:
    return sorted(
        (
            {
                "category_id": str(item.category_id),
                "planned_amount": canonical_decimal(item.planned_amount),
                "note": item.note,
            }
            for item in items
        ),
        key=lambda item: str(item["category_id"]),
    )


def _upsert_payload(data: BudgetUpsertRequest) -> dict[str, object]:
    return {
        "version": data.version,
        "planned_income": canonical_decimal(data.planned_income),
        "rollover_policy": data.rollover_policy,
        "allocations": _canonical_allocations(data.allocations),
    }


def _copy_payload(data: BudgetCopyRequest, source: date) -> dict[str, object]:
    return {
        "source_period": source.strftime("%Y-%m"),
        "overwrite": data.overwrite,
        "version": data.version,
    }


async def _is_frozen(session: AsyncSession, workspace_id: uuid.UUID, period: date) -> bool:
    return (
        await session.scalar(
            select(MonthClosure.id).where(
                MonthClosure.workspace_id == workspace_id,
                MonthClosure.period_month == period,
                MonthClosure.status == "confirmed",
            )
        )
        is not None
    )


def _raise_frozen(period: date) -> None:
    raise ApiError(
        status_code=409,
        code="BUDGET_PERIOD_FROZEN",
        message="Budget plan is frozen by Month Close",
        details={"period": period.strftime("%Y-%m")},
    )


def budget_plan_snapshot(
    budget: BudgetPeriod, allocations: list[BudgetAllocation]
) -> dict[str, object]:
    """Canonical aggregate serializer shared by audit and immutable plan revisions."""
    return {
        "id": str(budget.id),
        "workspace_id": str(budget.workspace_id),
        "period": budget.period_month.strftime("%Y-%m"),
        "currency": budget.currency,
        "planned_income": canonical_decimal(budget.planned_income),
        "rollover_policy": budget.rollover_policy,
        "version": budget.version,
        "deleted_at": budget.deleted_at.isoformat() if budget.deleted_at else None,
        "allocations": [
            {
                "id": str(item.id),
                "category_id": str(item.category_id),
                "planned_amount": canonical_decimal(item.planned_amount),
                "note": item.note,
            }
            for item in sorted(allocations, key=lambda item: (item.category_id, item.id))
        ],
    }


async def _categories(
    session: AsyncSession, workspace_id: uuid.UUID, category_ids: set[uuid.UUID]
) -> dict[uuid.UUID, Category]:
    if not category_ids:
        return {}
    return {
        item.id: item
        for item in (
            await session.scalars(
                select(Category).where(
                    Category.workspace_id == workspace_id,
                    Category.id.in_(category_ids),
                )
            )
        ).all()
    }


async def _validate_allocations(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    allocations: list[BudgetAllocationInput],
    *,
    code: str = "BUDGET_CATEGORY_INVALID",
) -> None:
    category_ids = [item.category_id for item in allocations]
    duplicates = sorted(
        {str(category_id) for category_id in category_ids if category_ids.count(category_id) > 1}
    )
    if duplicates:
        raise ApiError(
            status_code=422,
            code="BUDGET_ALLOCATION_INVALID",
            message="A category can occur only once in a budget plan",
            details={"category_ids": duplicates},
        )
    categories = await _categories(session, workspace_id, set(category_ids))
    invalid = sorted(
        str(category_id)
        for category_id in category_ids
        if (
            category_id not in categories
            or categories[category_id].deleted_at is not None
            or categories[category_id].is_archived
            or categories[category_id].category_type not in {"expense", "both"}
        )
    )
    if invalid:
        raise ApiError(
            status_code=409 if code != "BUDGET_CATEGORY_INVALID" else 422,
            code=code,
            message="Budget plan contains unavailable expense categories",
            details={"category_ids": invalid},
        )


async def _revision_replay(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    key: str,
    request_hash: str,
) -> BudgetGroupResponse | None:
    existing = await repository.revision_for_key(session, workspace_id, key)
    if existing is None:
        return None
    if existing.request_hash != request_hash:
        raise ApiError(
            status_code=409,
            code="BUDGET_IDEMPOTENCY_CONFLICT",
            message="Idempotency key was already used for another budget request",
        )
    return BudgetGroupResponse.model_validate(existing.response_snapshot)


async def _frozen_snapshot(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    period: date,
) -> dict[str, Any] | None:
    revision = await session.scalar(
        select(MonthCloseRevision)
        .join(MonthClosure, MonthClosure.current_revision_id == MonthCloseRevision.id)
        .where(
            MonthClosure.workspace_id == workspace_id,
            MonthClosure.period_month == period,
            MonthClosure.status == "confirmed",
            MonthCloseRevision.workspace_id == workspace_id,
        )
    )
    if revision is None:
        return None
    value = revision.snapshot.get("planning_budget")
    return dict(value) if isinstance(value, dict) else None


async def _previous_rollover(
    session: AsyncSession,
    workspace: Workspace,
    period: date,
    currency: str,
    policy: RolloverPolicy,
) -> BudgetRolloverResponse:
    predecessor_month = previous_month(period)
    if policy == "none":
        return BudgetRolloverResponse(amount=ZERO, policy=policy, provisional=False)

    predecessor_frozen = await _is_frozen(session, workspace.id, predecessor_month)
    predecessor = await repository.get_period(
        session, workspace.id, predecessor_month, currency, include_deleted=False
    )
    if predecessor is None:
        return BudgetRolloverResponse(
            amount=ZERO,
            policy=policy,
            provisional=not predecessor_frozen,
        )

    if predecessor_frozen:
        snapshot = await _frozen_snapshot(session, workspace.id, predecessor_month)
        if snapshot is not None:
            groups = snapshot.get("groups", [])
            group = next(
                (
                    item
                    for item in groups
                    if isinstance(item, dict) and item.get("currency") == currency
                ),
                None,
            )
            if isinstance(group, dict):
                previous_remaining = Decimal(str(group["remaining"]))
                return BudgetRolloverResponse(
                    amount=_apply_rollover(policy, previous_remaining),
                    policy=policy,
                    provisional=False,
                )

    predecessor_allocations = (await repository.allocations_for_periods(session, [predecessor.id]))[
        predecessor.id
    ]
    allocated = sum((item.planned_amount for item in predecessor_allocations), start=Decimal("0"))
    actuals = await project_budget_actuals(
        session, workspace.id, predecessor_month, workspace.timezone
    )
    actual_expense = actuals.get(currency, BudgetActual()).expense
    previous_remaining = money(allocated - actual_expense)
    return BudgetRolloverResponse(
        amount=_apply_rollover(policy, previous_remaining),
        policy=policy,
        provisional=not predecessor_frozen,
    )


def _apply_rollover(policy: str, remaining: Decimal) -> Decimal:
    if policy == "full":
        return money(remaining)
    if policy == "positive_only":
        return money(max(remaining, Decimal("0")))
    return ZERO


def _usage_percent(actual: Decimal, planned: Decimal) -> Decimal | None:
    if planned == 0:
        return None
    return money(actual * Decimal("100") / planned)


async def _live_group(
    session: AsyncSession,
    workspace: Workspace,
    budget: BudgetPeriod,
    allocations: list[BudgetAllocation],
    actuals: dict[str, BudgetActual],
    *,
    frozen: bool = False,
) -> BudgetGroupResponse:
    category_map = await _categories(
        session, workspace.id, {item.category_id for item in allocations}
    )
    actual = actuals.get(budget.currency, BudgetActual())
    allocated = money(sum((item.planned_amount for item in allocations), start=Decimal("0")))
    budgeted_actual = money(
        sum(
            (actual.category_expense.get(item.category_id, Decimal("0")) for item in allocations),
            start=Decimal("0"),
        )
    )
    rollover = await _previous_rollover(
        session,
        workspace,
        budget.period_month,
        budget.currency,
        cast(RolloverPolicy, budget.rollover_policy),
    )
    allocation_rows: list[BudgetAllocationProjection] = []
    for item in allocations:
        category = category_map.get(item.category_id)
        category_actual = money(actual.category_expense.get(item.category_id, Decimal("0")))
        planned = money(item.planned_amount)
        usage = _usage_percent(category_actual, planned)
        allocation_rows.append(
            BudgetAllocationProjection(
                id=item.id,
                category_id=item.category_id,
                category_name=category.name if category is not None else "Deleted category",
                parent_id=category.parent_id if category is not None else None,
                category_type=(
                    cast(Literal["income", "expense", "both"], category.category_type)
                    if category is not None
                    and category.category_type in {"income", "expense", "both"}
                    else "expense"
                ),
                category_archived=bool(category.is_archived) if category is not None else False,
                category_deleted=(category is None or category.deleted_at is not None),
                planned=planned,
                actual=category_actual,
                remaining=money(planned - category_actual),
                usage_percent=usage,
                note=item.note,
            )
        )
    planning_capacity = money(budget.planned_income + rollover.amount)
    return BudgetGroupResponse(
        id=budget.id,
        workspace_id=budget.workspace_id,
        period=budget.period_month.strftime("%Y-%m"),
        currency=budget.currency,
        frozen=frozen,
        projection_source="live",
        version=budget.version,
        deleted_at=budget.deleted_at,
        planned_income=money(budget.planned_income),
        rollover_policy=cast(RolloverPolicy, budget.rollover_policy),
        allocated=allocated,
        actual_income=actual.income,
        actual_expense=actual.expense,
        adjustment=actual.adjustment,
        actual_net_cashflow=actual.net_cashflow,
        budgeted_actual_expense=budgeted_actual,
        unbudgeted_actual_expense=money(actual.expense - budgeted_actual),
        remaining=money(allocated - actual.expense),
        rollover=rollover,
        planning_capacity=planning_capacity,
        unallocated=money(planning_capacity - allocated),
        allocations=allocation_rows,
    )


async def get_month(
    session: AsyncSession,
    workspace: Workspace,
    period: date,
    *,
    include_deleted: bool = False,
) -> BudgetMonthResponse:
    if await _is_frozen(session, workspace.id, period):
        snapshot = await _frozen_snapshot(session, workspace.id, period)
        if snapshot is None:
            return BudgetMonthResponse(
                period=period.strftime("%Y-%m"),
                timezone=workspace.timezone,
                projection_source="month_close_revision",
                historical_snapshot_available=False,
                groups=[],
            )
        groups = [
            BudgetGroupResponse.model_validate(item).model_copy(
                update={"frozen": True, "projection_source": "month_close_revision"}
            )
            for item in snapshot.get("groups", [])
            if isinstance(item, dict)
        ]
        return BudgetMonthResponse(
            period=str(snapshot.get("period", period.strftime("%Y-%m"))),
            timezone=str(snapshot.get("timezone", workspace.timezone)),
            projection_source="month_close_revision",
            groups=groups,
        )

    budgets = await repository.list_periods(
        session, workspace.id, period, include_deleted=include_deleted
    )
    allocations = await repository.allocations_for_periods(session, [item.id for item in budgets])
    actuals = await project_budget_actuals(session, workspace.id, period, workspace.timezone)
    groups = [
        await _live_group(
            session,
            workspace,
            item,
            allocations[item.id],
            actuals,
        )
        for item in budgets
    ]
    return BudgetMonthResponse(
        period=period.strftime("%Y-%m"),
        timezone=workspace.timezone,
        projection_source="live",
        groups=groups,
    )


async def get_group(
    session: AsyncSession,
    workspace: Workspace,
    period: date,
    currency: str,
    *,
    include_deleted: bool = False,
) -> BudgetGroupResponse:
    month = await get_month(session, workspace, period, include_deleted=include_deleted)
    group = next((item for item in month.groups if item.currency == currency), None)
    if group is None:
        raise ApiError(status_code=404, code="BUDGET_NOT_FOUND", message="Budget was not found")
    return group


async def _replace_allocations(
    session: AsyncSession,
    budget: BudgetPeriod,
    allocations: list[BudgetAllocationInput],
) -> list[BudgetAllocation]:
    await session.execute(
        delete(BudgetAllocation).where(BudgetAllocation.budget_period_id == budget.id)
    )
    rows = [
        BudgetAllocation(
            budget_period_id=budget.id,
            category_id=item.category_id,
            planned_amount=item.planned_amount,
            note=item.note,
        )
        for item in allocations
    ]
    session.add_all(rows)
    await session.flush()
    return rows


async def _record_success(
    session: AsyncSession,
    context: RequestContext,
    budget: BudgetPeriod,
    allocations: list[BudgetAllocation],
    *,
    action: Literal["create", "update", "delete", "restore", "copy"],
    key: str,
    request_hash: str,
    before: dict[str, object] | None,
    response: BudgetGroupResponse,
    audit_metadata: dict[str, object] | None = None,
) -> None:
    plan = budget_plan_snapshot(budget, allocations)
    revision = BudgetPlanRevision(
        workspace_id=context.workspace.id,
        budget_period_id=budget.id,
        revision_number=await repository.next_revision_number(session, budget.id),
        action=action,
        snapshot=plan,
        request_hash=request_hash,
        idempotency_key=key,
        response_snapshot=response.model_dump(mode="json"),
        actor_user_id=context.user.id,
        request_id=request_uuid(context.request_id),
        created_at=datetime.now(UTC),
    )
    session.add(revision)
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="budget_period",
        entity_id=budget.id,
        action=action,
        before_data=before,
        after_data={**plan, **(audit_metadata or {})},
        request_id=context.request_id,
    )
    await session.flush()


async def upsert(
    session: AsyncSession,
    context: RequestContext,
    period: date,
    currency: str,
    data: BudgetUpsertRequest,
    idempotency_key: str,
) -> BudgetGroupResponse:
    key = _idempotency_key(idempotency_key)
    payload = _upsert_payload(data)
    request_hash = _request_hash("upsert", context.workspace.id, period, currency, payload)
    await get_or_create_control(session, context.workspace.id, for_update=True)
    replay = await _revision_replay(session, context.workspace.id, key, request_hash)
    if replay is not None:
        return replay
    if await _is_frozen(session, context.workspace.id, period):
        _raise_frozen(period)
    existing = await repository.get_period(
        session,
        context.workspace.id,
        period,
        currency,
        include_deleted=True,
        for_update=True,
    )
    if existing is not None and existing.deleted_at is not None:
        raise ApiError(
            status_code=409,
            code="BUDGET_RESTORE_REQUIRED",
            message="Deleted Budget must be restored before it can be updated",
            details={"version": existing.version},
        )
    if existing is None and data.version is not None:
        raise ApiError(status_code=404, code="BUDGET_NOT_FOUND", message="Budget was not found")
    if existing is not None and data.version is None:
        raise ApiError(
            status_code=409,
            code="BUDGET_VERSION_CONFLICT",
            message="Budget version is required",
            details={"current_version": existing.version},
        )
    if existing is not None and existing.version != data.version:
        raise ApiError(
            status_code=409,
            code="BUDGET_VERSION_CONFLICT",
            message="Budget version is stale",
            details={"current_version": existing.version},
        )
    await _validate_allocations(session, context.workspace.id, data.allocations)
    now = datetime.now(UTC)
    action: Literal["create", "update"] = "create" if existing is None else "update"
    if existing is None:
        budget = BudgetPeriod(
            workspace_id=context.workspace.id,
            period_month=period,
            currency=currency,
            planned_income=data.planned_income,
            rollover_policy=data.rollover_policy,
            created_by=context.user.id,
            updated_by=context.user.id,
        )
        session.add(budget)
        await session.flush()
        before = None
    else:
        budget = existing
        current_allocations = (await repository.allocations_for_periods(session, [budget.id]))[
            budget.id
        ]
        before = budget_plan_snapshot(budget, current_allocations)
        budget.planned_income = data.planned_income
        budget.rollover_policy = data.rollover_policy
        budget.updated_by = context.user.id
        budget.updated_at = now
        budget.version += 1
    allocations = await _replace_allocations(session, budget, data.allocations)
    actuals = await project_budget_actuals(
        session, context.workspace.id, period, context.workspace.timezone
    )
    response = await _live_group(session, context.workspace, budget, allocations, actuals)
    await _record_success(
        session,
        context,
        budget,
        allocations,
        action=action,
        key=key,
        request_hash=request_hash,
        before=before,
        response=response,
    )
    await session.commit()
    return response


async def delete_period(
    session: AsyncSession,
    context: RequestContext,
    period: date,
    currency: str,
    version: int,
    idempotency_key: str,
) -> BudgetGroupResponse:
    key = _idempotency_key(idempotency_key)
    payload: dict[str, object] = {"version": version}
    request_hash = _request_hash("delete", context.workspace.id, period, currency, payload)
    await get_or_create_control(session, context.workspace.id, for_update=True)
    replay = await _revision_replay(session, context.workspace.id, key, request_hash)
    if replay is not None:
        return replay
    if await _is_frozen(session, context.workspace.id, period):
        _raise_frozen(period)
    budget = await repository.get_period(
        session,
        context.workspace.id,
        period,
        currency,
        include_deleted=True,
        for_update=True,
    )
    if budget is None or budget.deleted_at is not None:
        raise ApiError(status_code=404, code="BUDGET_NOT_FOUND", message="Budget was not found")
    if budget.version != version:
        raise ApiError(
            status_code=409,
            code="BUDGET_VERSION_CONFLICT",
            message="Budget version is stale",
            details={"current_version": budget.version},
        )
    allocations = (await repository.allocations_for_periods(session, [budget.id]))[budget.id]
    before = budget_plan_snapshot(budget, allocations)
    now = datetime.now(UTC)
    budget.deleted_at = now
    budget.updated_at = now
    budget.updated_by = context.user.id
    budget.version += 1
    await session.flush()
    actuals = await project_budget_actuals(
        session, context.workspace.id, period, context.workspace.timezone
    )
    response = await _live_group(session, context.workspace, budget, allocations, actuals)
    await _record_success(
        session,
        context,
        budget,
        allocations,
        action="delete",
        key=key,
        request_hash=request_hash,
        before=before,
        response=response,
    )
    await session.commit()
    return response


async def restore_period(
    session: AsyncSession,
    context: RequestContext,
    period: date,
    currency: str,
    version: int,
    idempotency_key: str,
) -> BudgetGroupResponse:
    key = _idempotency_key(idempotency_key)
    payload: dict[str, object] = {"version": version}
    request_hash = _request_hash("restore", context.workspace.id, period, currency, payload)
    await get_or_create_control(session, context.workspace.id, for_update=True)
    replay = await _revision_replay(session, context.workspace.id, key, request_hash)
    if replay is not None:
        return replay
    if await _is_frozen(session, context.workspace.id, period):
        _raise_frozen(period)
    budget = await repository.get_period(
        session,
        context.workspace.id,
        period,
        currency,
        include_deleted=True,
        for_update=True,
    )
    if budget is None or budget.deleted_at is None:
        raise ApiError(
            status_code=404, code="BUDGET_NOT_FOUND", message="Deleted Budget was not found"
        )
    if budget.version != version:
        raise ApiError(
            status_code=409,
            code="BUDGET_VERSION_CONFLICT",
            message="Budget version is stale",
            details={"current_version": budget.version},
        )
    allocations = (await repository.allocations_for_periods(session, [budget.id]))[budget.id]
    inputs = [
        BudgetAllocationInput(
            category_id=item.category_id,
            planned_amount=item.planned_amount,
            note=item.note,
        )
        for item in allocations
    ]
    await _validate_allocations(
        session,
        context.workspace.id,
        inputs,
        code="BUDGET_RESTORE_CATEGORY_CONFLICT",
    )
    before = budget_plan_snapshot(budget, allocations)
    budget.deleted_at = None
    budget.updated_at = datetime.now(UTC)
    budget.updated_by = context.user.id
    budget.version += 1
    await session.flush()
    actuals = await project_budget_actuals(
        session, context.workspace.id, period, context.workspace.timezone
    )
    response = await _live_group(session, context.workspace, budget, allocations, actuals)
    await _record_success(
        session,
        context,
        budget,
        allocations,
        action="restore",
        key=key,
        request_hash=request_hash,
        before=before,
        response=response,
    )
    await session.commit()
    return response


async def copy_period(
    session: AsyncSession,
    context: RequestContext,
    period: date,
    currency: str,
    data: BudgetCopyRequest,
    idempotency_key: str,
) -> BudgetGroupResponse:
    source_period = (
        parse_period(data.source_period) if data.source_period else previous_month(period)
    )
    key = _idempotency_key(idempotency_key)
    payload = _copy_payload(data, source_period)
    request_hash = _request_hash("copy", context.workspace.id, period, currency, payload)
    await get_or_create_control(session, context.workspace.id, for_update=True)
    replay = await _revision_replay(session, context.workspace.id, key, request_hash)
    if replay is not None:
        return replay
    if await _is_frozen(session, context.workspace.id, period):
        _raise_frozen(period)
    locked = await repository.lock_period_keys(
        session,
        context.workspace.id,
        {(source_period, currency), (period, currency)},
    )
    source = locked.get((source_period, currency))
    if source is None or source.deleted_at is not None:
        raise ApiError(
            status_code=404,
            code="BUDGET_COPY_SOURCE_NOT_FOUND",
            message="Source Budget was not found",
        )
    source_allocations = (await repository.allocations_for_periods(session, [source.id]))[source.id]
    inputs = [
        BudgetAllocationInput(
            category_id=item.category_id,
            planned_amount=item.planned_amount,
            note=item.note,
        )
        for item in source_allocations
    ]
    await _validate_allocations(
        session,
        context.workspace.id,
        inputs,
        code="BUDGET_COPY_CATEGORY_CONFLICT",
    )
    target = locked.get((period, currency))
    if target is not None and target.deleted_at is not None:
        raise ApiError(
            status_code=409,
            code="BUDGET_RESTORE_REQUIRED",
            message="Deleted target Budget must be restored before overwrite",
            details={"version": target.version},
        )
    if target is not None and not data.overwrite:
        raise ApiError(
            status_code=409,
            code="BUDGET_COPY_TARGET_EXISTS",
            message="Target Budget exists; explicit overwrite is required",
        )
    if target is not None and data.version != target.version:
        raise ApiError(
            status_code=409,
            code="BUDGET_VERSION_CONFLICT",
            message="Target Budget version is stale",
            details={"current_version": target.version},
        )
    now = datetime.now(UTC)
    if target is None:
        budget = BudgetPeriod(
            workspace_id=context.workspace.id,
            period_month=period,
            currency=currency,
            planned_income=source.planned_income,
            rollover_policy=source.rollover_policy,
            created_by=context.user.id,
            updated_by=context.user.id,
        )
        session.add(budget)
        await session.flush()
        before = None
    else:
        budget = target
        current_allocations = (await repository.allocations_for_periods(session, [budget.id]))[
            budget.id
        ]
        before = budget_plan_snapshot(budget, current_allocations)
        budget.planned_income = source.planned_income
        budget.rollover_policy = source.rollover_policy
        budget.updated_at = now
        budget.updated_by = context.user.id
        budget.version += 1
    allocations = await _replace_allocations(session, budget, inputs)
    actuals = await project_budget_actuals(
        session, context.workspace.id, period, context.workspace.timezone
    )
    response = await _live_group(session, context.workspace, budget, allocations, actuals)
    await _record_success(
        session,
        context,
        budget,
        allocations,
        action="copy",
        key=key,
        request_hash=request_hash,
        before=before,
        response=response,
        audit_metadata={
            "copy_source_budget_id": str(source.id),
            "copy_source_period": source_period.strftime("%Y-%m"),
        },
    )
    await session.commit()
    return response


async def list_history(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    period: date,
    currency: str,
    *,
    limit: int,
    offset: int,
) -> tuple[list[BudgetPlanRevisionResponse], int]:
    budget = await repository.get_period(
        session, workspace_id, period, currency, include_deleted=True
    )
    if budget is None:
        raise ApiError(status_code=404, code="BUDGET_NOT_FOUND", message="Budget was not found")
    revisions, total = await repository.list_revisions(
        session, workspace_id, budget.id, limit=limit, offset=offset
    )
    return [BudgetPlanRevisionResponse.model_validate(item) for item in revisions], total


async def planning_snapshot_for_close(
    session: AsyncSession,
    workspace: Workspace,
    period: date,
    _control: MonthCloseControl,
) -> tuple[dict[str, object], str]:
    """Lock Budget rows after control/closure and build the prospective close snapshot."""
    budgets = await repository.list_periods(
        session, workspace.id, period, include_deleted=False, for_update=True
    )
    allocations = await repository.allocations_for_periods(session, [item.id for item in budgets])
    actuals = await project_budget_actuals(session, workspace.id, period, workspace.timezone)
    groups: list[dict[str, Any]] = []
    for budget in budgets:
        group = await _live_group(
            session,
            workspace,
            budget,
            allocations[budget.id],
            actuals,
            frozen=False,
        )
        serialized = group.model_dump(mode="json")
        serialized["rollover"]["provisional"] = False
        groups.append(serialized)
    snapshot: dict[str, object] = {
        "schema_version": 1,
        "period": period.strftime("%Y-%m"),
        "timezone": workspace.timezone,
        "groups": groups,
    }
    return snapshot, hash_canonical(snapshot)
