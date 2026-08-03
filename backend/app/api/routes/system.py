from datetime import UTC, datetime

from fastapi import APIRouter

from app.core.config import settings
from app.schemas.system import SystemInfoResponse
from app.services.system import get_schema_version

router = APIRouter()


@router.get("/info", response_model=SystemInfoResponse)
async def system_info() -> SystemInfoResponse:
    """Return non-sensitive runtime and schema information."""
    return SystemInfoResponse(
        application=settings.project_name,
        environment=settings.environment,
        backend_version=settings.backend_version,
        debug=settings.debug,
        server_time=datetime.now(UTC),
        database_schema_version=await get_schema_version(),
    )
