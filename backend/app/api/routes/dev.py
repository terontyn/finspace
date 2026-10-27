from fastapi import APIRouter, Request

from app.dependencies.database import DbSession
from app.schemas.users import BootstrapResponse
from app.services.bootstrap import bootstrap_development

router = APIRouter()


@router.post("/bootstrap", response_model=BootstrapResponse)
async def bootstrap(request: Request, session: DbSession) -> BootstrapResponse:
    return await bootstrap_development(
        session, request_id=str(getattr(request.state, "request_id", ""))
    )
