from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session, get_forecast_session

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
ForecastDbSession = Annotated[AsyncSession, Depends(get_forecast_session)]
