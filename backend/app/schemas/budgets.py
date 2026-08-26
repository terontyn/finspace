import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field

from app.schemas.common import ApiModel, CurrencyCode, Money, PageMeta, PositiveMoney

RolloverPolicy = Literal["none", "positive_only", "full"]
BudgetProjectionSource = Literal["live", "month_close_revision"]


class BudgetAllocationInput(ApiModel):
    category_id: uuid.UUID
    planned_amount: PositiveMoney
    note: str | None = Field(default=None, max_length=1000)


class BudgetUpsertRequest(ApiModel):
    version: int | None = Field(default=None, ge=1)
    planned_income: Decimal = Field(ge=0, max_digits=20, decimal_places=4)
    rollover_policy: RolloverPolicy = "none"
    allocations: list[BudgetAllocationInput] = Field(default_factory=list, max_length=1000)


class BudgetVersionRequest(ApiModel):
    version: int = Field(ge=1)


class BudgetCopyRequest(ApiModel):
    source_period: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}$")
    overwrite: bool = False
    version: int | None = Field(default=None, ge=1)


class BudgetRolloverResponse(ApiModel):
    amount: Money
    policy: RolloverPolicy
    provisional: bool


class BudgetAllocationProjection(ApiModel):
    id: uuid.UUID
    category_id: uuid.UUID
    category_name: str
    parent_id: uuid.UUID | None
    category_type: Literal["income", "expense", "both"]
    category_archived: bool
    category_deleted: bool
    planned: Money
    actual: Money
    remaining: Money
    usage_percent: Decimal | None
    note: str | None


class BudgetGroupResponse(ApiModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    period: str
    currency: CurrencyCode
    frozen: bool
    projection_source: BudgetProjectionSource
    version: int
    deleted_at: datetime | None
    planned_income: Money
    rollover_policy: RolloverPolicy
    allocated: Money
    actual_income: Money
    actual_expense: Money
    adjustment: Money
    actual_net_cashflow: Money
    budgeted_actual_expense: Money
    unbudgeted_actual_expense: Money
    remaining: Money
    rollover: BudgetRolloverResponse
    planning_capacity: Money
    unallocated: Money
    allocations: list[BudgetAllocationProjection]


class BudgetMonthResponse(ApiModel):
    period: str
    timezone: str
    projection_source: BudgetProjectionSource
    historical_snapshot_available: bool = True
    groups: list[BudgetGroupResponse]


class BudgetPlanRevisionResponse(ApiModel):
    id: uuid.UUID
    budget_period_id: uuid.UUID
    revision_number: int
    action: Literal["create", "update", "delete", "restore", "copy"]
    snapshot: dict[str, object]
    actor_user_id: uuid.UUID
    request_id: uuid.UUID | None
    created_at: datetime


class BudgetPlanRevisionPage(ApiModel):
    items: list[BudgetPlanRevisionResponse]
    page: PageMeta
