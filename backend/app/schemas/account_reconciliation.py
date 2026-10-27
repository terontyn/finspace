import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import Field

from app.schemas.common import ApiModel, CurrencyCode, Money, PageMeta
from app.schemas.transactions import TransactionStatus, TransactionType


class AccountReconciliationPreviewRequest(ApiModel):
    statement_date: date
    statement_balance: Money
    currency: CurrencyCode
    account_version: int = Field(ge=1)


class AccountReconciliationConfirmRequest(AccountReconciliationPreviewRequest):
    preview_token: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=8, max_length=255)


class AccountReconciliationTransaction(ApiModel):
    id: uuid.UUID
    occurred_at: datetime
    transaction_type: TransactionType
    amount: Money
    signed_amount: Money
    currency: CurrencyCode
    status: TransactionStatus
    counterparty: str | None
    description: str | None
    version: int


class AccountReconciliationPreview(ApiModel):
    account_id: uuid.UUID
    statement_date: date
    cutoff_at: datetime
    statement_balance: Money
    calculated_balance: Money
    difference: Money
    currency: CurrencyCode
    account_version: int
    preview_token: str
    transactions: list[AccountReconciliationTransaction]


class AccountReconciliationResponse(ApiModel):
    id: uuid.UUID
    account_id: uuid.UUID
    statement_date: date
    statement_balance: Money
    calculated_balance: Money
    difference: Money
    currency: CurrencyCode
    status: Literal["confirmed"]
    account_version: int
    version: int
    created_by: uuid.UUID
    confirmed_by: uuid.UUID
    created_at: datetime
    confirmed_at: datetime
    transaction_ids: list[uuid.UUID]


class AccountReconciliationPage(ApiModel):
    items: list[AccountReconciliationResponse]
    page: PageMeta
