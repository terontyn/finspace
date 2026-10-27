from datetime import date

from fastapi import APIRouter

from app.dependencies.context import CurrentContext
from app.dependencies.database import DbSession
from app.schemas.common import CurrencyCode
from app.schemas.financial_reports import FinancialReportResponse
from app.services import reports as service

router = APIRouter()


@router.get("/financial", response_model=FinancialReportResponse)
async def financial_report(
    context: CurrentContext,
    session: DbSession,
    date_from: date,
    date_to: date,
    currency: CurrencyCode | None = None,
) -> FinancialReportResponse:
    return await service.financial_report(
        session,
        context.workspace,
        date_from=date_from,
        date_to=date_to,
        currency=currency,
    )
