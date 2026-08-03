from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: Literal["backend"]


class ReadinessChecks(BaseModel):
    database: Literal["ok"]
    redis: Literal["ok"]


class ReadinessResponse(BaseModel):
    status: Literal["ready"]
    checks: ReadinessChecks


class SystemInfoResponse(BaseModel):
    application: str
    environment: str
    backend_version: str
    debug: bool
    server_time: datetime
    database_schema_version: str
