import uuid
from datetime import date, datetime

from app.schemas.common import ApiModel, CurrencyCode, Money


class FinancialReportPeriod(ApiModel):
    date_from: date
    date_to: date
    cutoff_from: datetime
    cutoff_to: datetime
    timezone: str


class FinancialReportCategory(ApiModel):
    category_id: uuid.UUID | None
    name: str
    amount: Money
    transaction_count: int


class FinancialReportMonth(ApiModel):
    month: str
    income: Money
    expense: Money
    adjustment: Money
    net_cashflow: Money
    transactions_count: int


class FinancialReportExpense(ApiModel):
    transaction_id: uuid.UUID
    occurred_at: datetime
    amount: Money
    account_id: uuid.UUID
    account_name: str
    category_name: str
    counterparty: str | None
    description: str | None


class FinancialReportGroup(ApiModel):
    currency: CurrencyCode
    income: Money
    expense: Money
    adjustment: Money
    net_cashflow: Money
    transfer_volume: Money
    transactions_count: int
    spending_by_category: list[FinancialReportCategory]
    monthly_comparison: list[FinancialReportMonth]
    largest_expenses: list[FinancialReportExpense]


class FinancialReportResponse(ApiModel):
    period: FinancialReportPeriod
    groups: list[FinancialReportGroup]
