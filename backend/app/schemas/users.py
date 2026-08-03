import uuid
from datetime import datetime

from app.schemas.common import ApiModel, CurrencyCode


class UserResponse(ApiModel):
    id: uuid.UUID
    email: str
    display_name: str
    locale: str
    timezone: str
    is_active: bool
    version: int


class WorkspaceResponse(ApiModel):
    id: uuid.UUID
    name: str
    base_currency: CurrencyCode
    timezone: str
    owner_user_id: uuid.UUID
    version: int


class WorkspaceMemberResponse(ApiModel):
    user_id: uuid.UUID
    email: str
    display_name: str
    role: str
    created_at: datetime


class BootstrapResponse(ApiModel):
    user_id: uuid.UUID
    workspace_id: uuid.UUID
    created: bool
