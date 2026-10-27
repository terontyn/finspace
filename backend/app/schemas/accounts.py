import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, field_validator

from app.schemas.common import ApiModel, CurrencyCode, Money, PageMeta, require_timezone

AccountType = Literal[
    "cash",
    "debit_card",
    "credit_card",
    "current_account",
    "savings",
    "deposit",
    "brokerage",
    "crypto_wallet",
    "other",
]


class AccountCreate(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    account_type: AccountType
    currency: CurrencyCode
    institution: str | None = Field(default=None, max_length=200)
    opening_balance: Money = Decimal("0")
    opening_balance_at: datetime
    credit_limit: Money | None = None
    description: str | None = None

    @field_validator("opening_balance_at")
    @classmethod
    def validate_opening_balance_at(cls, value: datetime) -> datetime:
        return require_timezone(value)


class AccountUpdate(ApiModel):
    version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    account_type: AccountType | None = None
    currency: CurrencyCode | None = None
    institution: str | None = Field(default=None, max_length=200)
    opening_balance: Money | None = None
    opening_balance_at: datetime | None = None
    credit_limit: Money | None = None
    description: str | None = None
    is_archived: bool | None = None

    @field_validator("opening_balance_at")
    @classmethod
    def validate_opening_balance_at(cls, value: datetime | None) -> datetime | None:
        return require_timezone(value) if value is not None else None


class VersionRequest(ApiModel):
    version: int = Field(ge=1)


class AccountResponse(ApiModel):
    id: uuid.UUID
    name: str
    account_type: AccountType
    currency: CurrencyCode
    institution: str | None
    opening_balance: Money
    opening_balance_at: datetime
    credit_limit: Money | None
    description: str | None
    is_archived: bool
    version: int
    created_at: datetime
    updated_at: datetime


class AccountPage(ApiModel):
    items: list[AccountResponse]
    page: PageMeta


class AccountBalance(ApiModel):
    account_id: uuid.UUID
    name: str
    currency: CurrencyCode
    opening_balance: Money
    balance: Money
