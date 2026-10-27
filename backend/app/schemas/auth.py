import uuid
from typing import Literal

from pydantic import Field

from app.schemas.common import ApiModel, CurrencyCode
from app.schemas.users import UserResponse, WorkspaceResponse


class RegisterRequest(ApiModel):
    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=10, max_length=128)
    workspace_name: str = Field(min_length=1, max_length=200)
    base_currency: CurrencyCode = "RUB"
    timezone: str = Field(default="Europe/Amsterdam", min_length=1, max_length=100)


class LoginRequest(ApiModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=128)


class SetDevelopmentPasswordRequest(ApiModel):
    user_id: uuid.UUID
    password: str = Field(min_length=10, max_length=128)


class AuthResponse(ApiModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: UserResponse
    workspace: WorkspaceResponse


class AuthMeResponse(ApiModel):
    user: UserResponse
    workspace: WorkspaceResponse
    role: str


class LogoutResponse(ApiModel):
    revoked: bool
