from fastapi import APIRouter, Query

from app.dependencies.context import CurrentContext, OwnerContext
from app.dependencies.database import DbSession
from app.schemas.automations import (
    MonthCloseConfirmRequest,
    MonthCloseReopenRequest,
    MonthClosurePage,
    MonthClosureResponse,
)
from app.schemas.common import PageMeta
from app.services import month_close as service

router = APIRouter()


@router.get("", response_model=MonthClosurePage)
async def month_close_list(
    context: CurrentContext,
    session: DbSession,
    limit: int = Query(default=24, ge=1, le=120),
    offset: int = Query(default=0, ge=0),
) -> MonthClosurePage:
    items, total = await service.list_closures(
        session, context.workspace.id, limit=limit, offset=offset
    )
    return MonthClosurePage(
        items=[MonthClosureResponse.model_validate(item) for item in items],
        page=PageMeta(limit=limit, offset=offset, total=total),
    )


@router.get("/{year}/{month}", response_model=MonthClosureResponse)
async def month_close_get(
    year: int,
    month: int,
    context: CurrentContext,
    session: DbSession,
) -> MonthClosureResponse:
    period = service.period_date(year, month)
    return MonthClosureResponse.model_validate(
        await service.get_closure(session, context.workspace.id, period)
    )


@router.post("/{year}/{month}/prepare", response_model=MonthClosureResponse)
async def month_close_prepare(
    year: int,
    month: int,
    context: CurrentContext,
    session: DbSession,
) -> MonthClosureResponse:
    period = service.period_date(year, month)
    closure = await service.prepare(
        session,
        context.workspace,
        period,
        actor_user_id=context.user.id,
        request_id=context.request_id,
        source="api",
    )
    return MonthClosureResponse.model_validate(closure)


@router.post("/{year}/{month}/confirm", response_model=MonthClosureResponse)
async def month_close_confirm(
    year: int,
    month: int,
    data: MonthCloseConfirmRequest,
    context: OwnerContext,
    session: DbSession,
) -> MonthClosureResponse:
    period = service.period_date(year, month)
    return MonthClosureResponse.model_validate(
        await service.confirm(
            session,
            context,
            period,
            version=data.version,
            explicit=data.confirm,
        )
    )


@router.post("/{year}/{month}/reopen", response_model=MonthClosureResponse)
async def month_close_reopen(
    year: int,
    month: int,
    data: MonthCloseReopenRequest,
    context: OwnerContext,
    session: DbSession,
) -> MonthClosureResponse:
    period = service.period_date(year, month)
    return MonthClosureResponse.model_validate(
        await service.reopen(
            session,
            context,
            period,
            version=data.version,
            reason=data.reason,
        )
    )
