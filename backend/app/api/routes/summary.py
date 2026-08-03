from datetime import datetime

from fastapi import APIRouter

from app.dependencies.context import CurrentContext
from app.dependencies.database import DbSession
from app.schemas.transactions import FinancialSummaryResponse
from app.services.calculations import calculate_summary

router = APIRouter()


@router.get("/financial-summary", response_model=FinancialSummaryResponse)
async def financial_summary(
    context: CurrentContext,
    session: DbSession,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> FinancialSummaryResponse:
    return await calculate_summary(
        session,
        context.workspace.id,
        date_from=date_from,
        date_to=date_to,
    )
