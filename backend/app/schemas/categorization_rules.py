import uuid
from datetime import datetime
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from app.schemas.common import ApiModel, PageMeta
from app.schemas.transactions import EntityRef, TransactionResponse

CategorizationTransactionType = Literal["income", "expense", "refund", "adjustment"]
CategorizationApplyReason = Literal["applied", "no_match", "already_categorized"]


class CategorizationRuleCreate(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    priority: int = Field(default=100, ge=0)
    is_active: bool = True
    transaction_type: CategorizationTransactionType | None = None
    account_id: uuid.UUID | None = None
    payee_id: uuid.UUID | None = None
    counterparty_contains: str | None = Field(default=None, max_length=300)
    description_contains: str | None = Field(default=None, max_length=300)
    category_id: uuid.UUID

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Rule name must contain visible characters")
        return value

    @field_validator("counterparty_contains", "description_contains")
    @classmethod
    def normalize_match_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Match text must contain visible characters")
        return value

    @model_validator(mode="after")
    def require_matcher(self) -> Self:
        if not any(
            (
                self.transaction_type is not None,
                self.account_id is not None,
                self.payee_id is not None,
                self.counterparty_contains is not None,
                self.description_contains is not None,
            )
        ):
            raise ValueError("At least one categorization rule matcher is required")
        return self


class CategorizationRuleUpdate(ApiModel):
    version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    priority: int | None = Field(default=None, ge=0)
    is_active: bool | None = None
    transaction_type: CategorizationTransactionType | None = None
    account_id: uuid.UUID | None = None
    payee_id: uuid.UUID | None = None
    counterparty_contains: str | None = Field(default=None, max_length=300)
    description_contains: str | None = Field(default=None, max_length=300)
    category_id: uuid.UUID | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Rule name must contain visible characters")
        return value

    @field_validator("counterparty_contains", "description_contains")
    @classmethod
    def normalize_match_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Match text must contain visible characters")
        return value


class CategorizationRuleResponse(ApiModel):
    id: uuid.UUID
    name: str
    priority: int
    is_active: bool
    transaction_type: CategorizationTransactionType | None
    account_id: uuid.UUID | None
    payee_id: uuid.UUID | None
    counterparty_contains: str | None
    description_contains: str | None
    category_id: uuid.UUID
    version: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class CategorizationRulePage(ApiModel):
    items: list[CategorizationRuleResponse]
    page: PageMeta


class CategorizationPreviewRequest(ApiModel):
    transaction_id: uuid.UUID


class CategorizationPreviewResponse(ApiModel):
    matched: bool
    rule: CategorizationRuleResponse | None
    category: EntityRef | None


class CategorizationApplyRequest(ApiModel):
    version: int = Field(ge=1)


class CategorizationApplyResponse(ApiModel):
    applied: bool
    reason: CategorizationApplyReason
    rule: CategorizationRuleResponse | None
    category: EntityRef | None
    transaction: TransactionResponse
