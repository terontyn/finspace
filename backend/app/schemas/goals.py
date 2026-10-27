import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from app.schemas.common import (
    ApiModel,
    CurrencyCode,
    Money,
    PageMeta,
    PositiveMoney,
    require_timezone,
)

GoalStatus = Literal["active", "paused", "completed", "cancelled"]


class GoalCreate(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    currency: CurrencyCode
    target_amount: PositiveMoney
    target_date: date | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Goal name must not be blank")
        return normalized


class GoalUpdate(ApiModel):
    version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    currency: CurrencyCode | None = None
    target_amount: PositiveMoney | None = None
    target_date: date | None = None

    @model_validator(mode="after")
    def validate_patch(self) -> Self:
        mutable = self.model_fields_set - {"version"}
        if not mutable:
            raise ValueError("At least one mutable Goal field is required")
        for field in ("name", "currency", "target_amount"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        if "name" in self.model_fields_set and self.name is not None:
            self.name = self.name.strip()
            if not self.name:
                raise ValueError("Goal name must not be blank")
        return self


class GoalVersionRequest(ApiModel):
    version: int = Field(ge=1)


class GoalContributionCreate(ApiModel):
    amount: PositiveMoney
    note: str | None = Field(default=None, max_length=1000)
    contributed_at: datetime | None = None

    @field_validator("contributed_at")
    @classmethod
    def validate_contributed_at(cls, value: datetime | None) -> datetime | None:
        return require_timezone(value) if value is not None else None


class GoalCorrectionCreate(ApiModel):
    adjustment_amount: Money
    note: str | None = Field(default=None, max_length=1000)
    contributed_at: datetime | None = None

    @field_validator("adjustment_amount")
    @classmethod
    def validate_adjustment(cls, value: Decimal) -> Decimal:
        if value == 0:
            raise ValueError("Correction adjustment must not be zero")
        return value

    @field_validator("contributed_at")
    @classmethod
    def validate_contributed_at(cls, value: datetime | None) -> datetime | None:
        return require_timezone(value) if value is not None else None


class GoalResponse(ApiModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    description: str | None
    currency: CurrencyCode
    target_amount: Money
    target_date: date | None
    status: GoalStatus
    version: int
    deleted_at: datetime | None
    created_by: uuid.UUID
    updated_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    contributed_amount: Money
    remaining_amount: Money
    progress_percent: Decimal
    is_target_reached: bool
    contribution_count: int
    days_remaining: int | None
    overdue: bool


class GoalPage(ApiModel):
    items: list[GoalResponse]
    page: PageMeta


class GoalContributionResponse(ApiModel):
    id: uuid.UUID
    goal_id: uuid.UUID
    workspace_id: uuid.UUID
    currency: CurrencyCode
    amount: Money
    note: str | None
    contributed_at: datetime
    correction_of_id: uuid.UUID | None
    created_by: uuid.UUID
    created_by_display_name: str | None = None
    created_at: datetime


class GoalContributionPage(ApiModel):
    items: list[GoalContributionResponse]
    page: PageMeta


class GoalContributionCommandResponse(ApiModel):
    goal: GoalResponse
    contribution: GoalContributionResponse
