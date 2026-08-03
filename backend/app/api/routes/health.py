import asyncio
from typing import Literal

from fastapi import APIRouter

from app.core.errors import ApiError
from app.schemas.system import HealthResponse, ReadinessChecks, ReadinessResponse
from app.services.system import check_database, check_redis

router = APIRouter()


@router.get("", response_model=HealthResponse, summary="Liveness probe")
async def health() -> HealthResponse:
    """Report that the API process is alive without touching dependencies."""
    return HealthResponse(status="ok", service="backend")


@router.get("/ready", response_model=ReadinessResponse, summary="Readiness probe")
async def readiness() -> ReadinessResponse:
    """Report whether PostgreSQL and Redis can accept requests."""
    database_result, redis_result = await asyncio.gather(
        check_database(), check_redis(), return_exceptions=True
    )
    checks: dict[str, Literal["ok", "unavailable"]] = {
        "database": "unavailable" if isinstance(database_result, BaseException) else "ok",
        "redis": "unavailable" if isinstance(redis_result, BaseException) else "ok",
    }

    if "unavailable" in checks.values():
        unavailable = [name for name, status in checks.items() if status == "unavailable"]
        raise ApiError(
            status_code=503,
            code="DEPENDENCY_UNAVAILABLE",
            message=f"Unavailable dependencies: {', '.join(unavailable)}",
            details={"checks": checks},
        )

    return ReadinessResponse(
        status="ready",
        checks=ReadinessChecks(database="ok", redis="ok"),
    )
