import uuid
from datetime import datetime

from pydantic import Field, field_validator, model_validator

from app.schemas.common import ApiModel, PageMeta


def _display_value(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("Value must contain visible characters")
    return stripped


class PayeeCreate(ApiModel):
    name: str = Field(min_length=1, max_length=300)
    notes: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _display_value(value)


class PayeeUpdate(ApiModel):
    version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=300)
    notes: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        return _display_value(value) if value is not None else None

    @model_validator(mode="after")
    def reject_explicit_null_name(self) -> "PayeeUpdate":
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name cannot be null")
        return self


class PayeeAliasCreate(ApiModel):
    version: int = Field(ge=1)
    alias: str = Field(min_length=1, max_length=300)

    @field_validator("alias")
    @classmethod
    def validate_alias(cls, value: str) -> str:
        return _display_value(value)


class PayeeAliasResponse(ApiModel):
    id: uuid.UUID
    alias: str
    is_primary: bool
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class PayeeResponse(ApiModel):
    id: uuid.UUID
    name: str
    notes: str | None
    aliases: list[PayeeAliasResponse]
    version: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class PayeePage(ApiModel):
    items: list[PayeeResponse]
    page: PageMeta
