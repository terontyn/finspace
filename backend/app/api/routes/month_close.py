from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.automations import MonthCloseControl, MonthClosure
from app.dependencies.context import (
    CurrentContext,
    EditorContext,
    OwnerContext,
    RequestContext,
)
from app.dependencies.database import DbSession
from app.schemas.automations import (
    MonthCloseAsClosedReport,
    MonthCloseComparisonResponse,
    MonthCloseConfirmRequest,
    MonthCloseHistoryPage,
    MonthClosePeriodSummary,
    MonthCloseReopenRequest,
    MonthCloseRevisionResponse,
    MonthClosurePage,
    MonthClosureResponse,
)
from app.schemas.common import PageMeta
from app.services import month_close as service

router = APIRouter()


async def _closure_response(
    session: AsyncSession,
    context: RequestContext,
    closure: MonthClosure,
    control: MonthCloseControl | None = None,
) -> MonthClosureResponse:
    typed_control = control or await service.read_control(session, context.workspace.id)
    base = MonthClosureResponse.model_validate(closure).model_dump()
    base["current_revision"] = await service.current_revision_number(
        session,
        context.workspace.id,
        base["current_revision_id"],
    )
    base["capabilities"] = service.capabilities(
        role=context.role,
        workspace=context.workspace,
        period=base["period_month"],
        status=base["status"],
        control=typed_control,
    )
    return MonthClosureResponse.model_validate(base)


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
    control = await service.read_control(session, context.workspace.id)
    responses = [await _closure_response(session, context, item, control) for item in items]
    return MonthClosurePage(
        items=responses,
        periods=[
            MonthClosePeriodSummary.model_validate(item)
            for item in await service.list_period_summaries(
                session, context.workspace, context.role, items, control
            )
        ],
        closed_through=control.closed_through,
        backup_policy=("require_healthy" if control.backup_policy == "require_healthy" else "warn"),
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
    return await _closure_response(
        session,
        context,
        await service.get_closure(session, context.workspace.id, period),
    )


@router.get("/{year}/{month}/history", response_model=MonthCloseHistoryPage)
async def month_close_history(
    year: int,
    month: int,
    context: CurrentContext,
    session: DbSession,
    order: Literal["newest", "oldest"] = Query(default="newest"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> MonthCloseHistoryPage:
    period = service.period_date(year, month)
    closure, items, total = await service.list_history(
        session,
        context.workspace.id,
        period,
        order=order,
        limit=limit,
        offset=offset,
    )
    return MonthCloseHistoryPage(
        closure=await _closure_response(session, context, closure),
        items=[MonthCloseRevisionResponse.model_validate(item) for item in items],
        page=PageMeta(limit=limit, offset=offset, total=total),
        order=order,
    )


@router.get(
    "/{year}/{month}/history/{revision_number}",
    response_model=MonthCloseRevisionResponse,
)
async def month_close_revision(
    year: int,
    month: int,
    revision_number: int,
    context: CurrentContext,
    session: DbSession,
) -> MonthCloseRevisionResponse:
    return MonthCloseRevisionResponse.model_validate(
        await service.revision_detail(
            session,
            context.workspace.id,
            service.period_date(year, month),
            revision_number,
        )
    )


@router.get(
    "/{year}/{month}/history/{revision_number}/report",
    response_model=MonthCloseAsClosedReport,
)
async def month_close_revision_report(
    year: int,
    month: int,
    revision_number: int,
    context: CurrentContext,
    session: DbSession,
) -> MonthCloseAsClosedReport:
    return MonthCloseAsClosedReport.model_validate(
        await service.as_closed_report(
            session,
            context.workspace.id,
            service.period_date(year, month),
            revision_number,
        )
    )


@router.get(
    "/{year}/{month}/history/{revision_number}/comparison",
    response_model=MonthCloseComparisonResponse,
)
async def month_close_revision_comparison(
    year: int,
    month: int,
    revision_number: int,
    context: CurrentContext,
    session: DbSession,
) -> MonthCloseComparisonResponse:
    return MonthCloseComparisonResponse.model_validate(
        await service.compare_with_current(
            session,
            context.workspace,
            service.period_date(year, month),
            revision_number,
        )
    )


@router.post("/{year}/{month}/prepare", response_model=MonthClosureResponse)
async def month_close_prepare(
    year: int,
    month: int,
    context: EditorContext,
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
    return await _closure_response(session, context, closure)


@router.post("/{year}/{month}/confirm", response_model=MonthClosureResponse)
async def month_close_confirm(
    year: int,
    month: int,
    data: MonthCloseConfirmRequest,
    context: OwnerContext,
    session: DbSession,
    idempotency_key: Annotated[str | None, Header(alias="X-Idempotency-Key")] = None,
) -> MonthClosureResponse:
    period = service.period_date(year, month)
    return await _closure_response(
        session,
        context,
        await service.confirm(
            session,
            context,
            period,
            version=data.version,
            explicit=data.confirm,
            prepare_token=data.prepare_token,
            idempotency_key=idempotency_key or "",
        ),
    )


@router.post("/{year}/{month}/reopen", response_model=MonthClosureResponse)
async def month_close_reopen(
    year: int,
    month: int,
    data: MonthCloseReopenRequest,
    context: OwnerContext,
    session: DbSession,
    idempotency_key: Annotated[str | None, Header(alias="X-Idempotency-Key")] = None,
) -> MonthClosureResponse:
    period = service.period_date(year, month)
    return await _closure_response(
        session,
        context,
        await service.reopen(
            session,
            context,
            period,
            version=data.version,
            reason=data.reason,
            idempotency_key=idempotency_key or "",
        ),
    )
