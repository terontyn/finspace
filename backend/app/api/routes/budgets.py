from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Header, Query

from app.dependencies.context import CurrentContext, EditorContext
from app.dependencies.database import DbSession, ForecastDbSession
from app.schemas.budget_forecasts import BudgetForecastResponse
from app.schemas.budgets import (
    BudgetCopyRequest,
    BudgetGroupResponse,
    BudgetMonthResponse,
    BudgetPlanRevisionPage,
    BudgetUpsertRequest,
    BudgetVersionRequest,
)
from app.schemas.common import CurrencyCode, PageMeta
from app.services import budget_forecasts as forecast_service
from app.services import budgets as service

router = APIRouter()
IdempotencyHeader = Annotated[str | None, Header(alias="X-Idempotency-Key")]


@router.get("/{period}", response_model=BudgetMonthResponse)
async def budget_month(
    period: str,
    context: CurrentContext,
    session: DbSession,
    include_deleted: bool = False,
) -> BudgetMonthResponse:
    return await service.get_month(
        session,
        context.workspace,
        service.parse_period(period),
        include_deleted=include_deleted,
    )


@router.get("/{period}/{currency}", response_model=BudgetGroupResponse)
async def budget_get(
    period: str,
    currency: CurrencyCode,
    context: CurrentContext,
    session: DbSession,
    include_deleted: bool = False,
) -> BudgetGroupResponse:
    return await service.get_group(
        session,
        context.workspace,
        service.parse_period(period),
        currency,
        include_deleted=include_deleted,
    )


@router.get("/{period}/{currency}/forecast", response_model=BudgetForecastResponse)
async def budget_forecast(
    period: str,
    currency: CurrencyCode,
    context: CurrentContext,
    session: ForecastDbSession,
    include_occurrences: bool = False,
) -> BudgetForecastResponse:
    return await forecast_service.get_forecast(
        session,
        context.workspace,
        service.parse_period(period),
        currency,
        as_of=datetime.now(UTC).replace(microsecond=0),
        include_occurrences=include_occurrences,
    )


@router.put("/{period}/{currency}", response_model=BudgetGroupResponse)
async def budget_put(
    period: str,
    currency: CurrencyCode,
    data: BudgetUpsertRequest,
    context: EditorContext,
    session: DbSession,
    idempotency_key: IdempotencyHeader = None,
) -> BudgetGroupResponse:
    return await service.upsert(
        session,
        context,
        service.parse_period(period),
        currency,
        data,
        idempotency_key or "",
    )


@router.delete("/{period}/{currency}", response_model=BudgetGroupResponse)
async def budget_delete(
    period: str,
    currency: CurrencyCode,
    version: int,
    context: EditorContext,
    session: DbSession,
    idempotency_key: IdempotencyHeader = None,
) -> BudgetGroupResponse:
    return await service.delete_period(
        session,
        context,
        service.parse_period(period),
        currency,
        version,
        idempotency_key or "",
    )


@router.post("/{period}/{currency}/restore", response_model=BudgetGroupResponse)
async def budget_restore(
    period: str,
    currency: CurrencyCode,
    data: BudgetVersionRequest,
    context: EditorContext,
    session: DbSession,
    idempotency_key: IdempotencyHeader = None,
) -> BudgetGroupResponse:
    return await service.restore_period(
        session,
        context,
        service.parse_period(period),
        currency,
        data.version,
        idempotency_key or "",
    )


@router.post("/{period}/{currency}/copy", response_model=BudgetGroupResponse)
async def budget_copy(
    period: str,
    currency: CurrencyCode,
    data: BudgetCopyRequest,
    context: EditorContext,
    session: DbSession,
    idempotency_key: IdempotencyHeader = None,
) -> BudgetGroupResponse:
    return await service.copy_period(
        session,
        context,
        service.parse_period(period),
        currency,
        data,
        idempotency_key or "",
    )


@router.get("/{period}/{currency}/history", response_model=BudgetPlanRevisionPage)
async def budget_history(
    period: str,
    currency: CurrencyCode,
    context: CurrentContext,
    session: DbSession,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> BudgetPlanRevisionPage:
    items, total = await service.list_history(
        session,
        context.workspace.id,
        service.parse_period(period),
        currency,
        limit=limit,
        offset=offset,
    )
    return BudgetPlanRevisionPage(
        items=items,
        page=PageMeta(limit=limit, offset=offset, total=total),
    )
