import uuid
from datetime import datetime
from typing import Any

from app.schemas.common import ApiModel, PageMeta


class AuditResponse(ApiModel):
    id: uuid.UUID
    workspace_id: uuid.UUID | None
    actor_user_id: uuid.UUID | None
    entity_type: str
    entity_id: uuid.UUID
    action: str
    before_data: dict[str, Any] | None
    after_data: dict[str, Any] | None
    request_id: uuid.UUID | None
    source: str
    created_at: datetime


class AuditPage(ApiModel):
    items: list[AuditResponse]
    page: PageMeta
