import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from app.schemas.common import (
    ApiModel,
    CurrencyCode,
    Money,
    PageMeta,
    PositiveMoney,
    require_timezone,
)

TransactionType = Literal["income", "expense", "transfer", "refund", "adjustment"]
TransactionStatus = Literal["draft", "confirmed", "reconciled", "cancelled"]
TransactionSource = Literal[
    "manual", "api", "import", "system", "google_sheets", "automation", "telegram"
]


class SplitInput(ApiModel):
    category_id: uuid.UUID
    amount: PositiveMoney
    comment: str | None = None


class TransactionCreate(ApiModel):
    occurred_at: datetime
    transaction_type: TransactionType
    amount: PositiveMoney
    currency: CurrencyCode
    account_id: uuid.UUID
    target_account_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    payee_id: uuid.UUID | None = None
    counterparty: str | None = Field(default=None, max_length=300)
    description: str | None = None
    comment: str | None = None
    status: TransactionStatus = "confirmed"
    source: TransactionSource = "manual"
    related_transaction_id: uuid.UUID | None = None
    external_id: str | None = Field(default=None, max_length=300)
    splits: list[SplitInput] = Field(default_factory=list)

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: datetime) -> datetime:
        return require_timezone(value)


class TransactionUpdate(ApiModel):
    version: int = Field(ge=1)
    occurred_at: datetime | None = None
    transaction_type: TransactionType | None = None
    amount: PositiveMoney | None = None
    currency: CurrencyCode | None = None
    account_id: uuid.UUID | None = None
    target_account_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    payee_id: uuid.UUID | None = None
    counterparty: str | None = Field(default=None, max_length=300)
    description: str | None = None
    comment: str | None = None
    status: TransactionStatus | None = None
    related_transaction_id: uuid.UUID | None = None
    external_id: str | None = Field(default=None, max_length=300)
    splits: list[SplitInput] | None = None

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: datetime | None) -> datetime | None:
        return require_timezone(value) if value is not None else None


class EntityRef(ApiModel):
    id: uuid.UUID
    name: str


class SplitResponse(ApiModel):
    id: uuid.UUID
    category_id: uuid.UUID
    category_name: str
    amount: Money
    comment: str | None


class TransactionResponse(ApiModel):
    id: uuid.UUID
    occurred_at: datetime
    transaction_type: TransactionType
    amount: Money
    currency: CurrencyCode
    account: EntityRef
    target_account: EntityRef | None
    category: EntityRef | None
    payee: EntityRef | None
    counterparty: str | None
    description: str | None
    comment: str | None
    status: TransactionStatus
    source: TransactionSource
    related_transaction_id: uuid.UUID | None
    external_id: str | None
    splits: list[SplitResponse]
    version: int
    created_at: datetime
    updated_at: datetime


class TransactionPage(ApiModel):
    items: list[TransactionResponse]
    page: PageMeta


class FinancialSummaryGroup(ApiModel):
    currency: CurrencyCode
    income: Money
    expense: Money
    net_cashflow: Money
    transfer_volume: Money
    transactions_count: int


class FinancialSummaryResponse(ApiModel):
    groups: list[FinancialSummaryGroup]
