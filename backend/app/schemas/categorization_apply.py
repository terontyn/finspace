import uuid
from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from app.schemas.common import ApiModel, PageMeta

MAX_APPLY_ITEMS = 100

CategorizationApplyStatus = Literal[
    "applied",
    "transaction_changed",
    "rule_changed",
    "category_changed",
    "already_categorized",
    "split",
    "transfer",
    "reconciled",
    "closed_period",
    "no_match",
    "not_found",
    "failed",
]
CategorizationApplyOperationStatus = Literal["in_progress", "completed"]


class CategorizationApplyRequest(ApiModel):
    item_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_APPLY_ITEMS)

    @model_validator(mode="after")
    def reject_duplicates(self) -> Self:
        # Duplicates are rejected rather than deduplicated so the requested count always equals the
        # number of persisted results, and so the canonical hash cannot be ambiguous.
        if len(set(self.item_ids)) != len(self.item_ids):
            raise ValueError("Duplicate preview item identifiers are not allowed")
        return self


class CategorizationApplyItemResult(ApiModel):
    item_id: uuid.UUID
    transaction_id: uuid.UUID | None
    status: CategorizationApplyStatus
    error_code: str | None = None
    transaction_version: int | None = None
    expected_version: int | None = None
    current_version: int | None = None


class CategorizationApplySummary(ApiModel):
    requested: int
    applied: int
    conflicts: int
    not_applied: int
    failed: int


class CategorizationApplyResponse(ApiModel):
    preview_id: uuid.UUID
    operation_id: uuid.UUID
    summary: CategorizationApplySummary
    results: list[CategorizationApplyItemResult]


class CategorizationApplyHistoryActor(ApiModel):
    actor_user_id: uuid.UUID
    display_name: str | None


class CategorizationApplyHistoryCounts(ApiModel):
    applied: int = 0
    transaction_changed: int = 0
    rule_changed: int = 0
    category_changed: int = 0
    already_categorized: int = 0
    split: int = 0
    transfer: int = 0
    reconciled: int = 0
    closed_period: int = 0
    no_match: int = 0
    not_found: int = 0
    failed: int = 0


class CategorizationApplyOperationHistory(ApiModel):
    id: uuid.UUID
    actor: CategorizationApplyHistoryActor
    status: CategorizationApplyOperationStatus
    requested_count: int
    result_count: int
    counts: CategorizationApplyHistoryCounts
    created_at: datetime
    completed_at: datetime | None


class CategorizationApplyOperationHistoryPage(ApiModel):
    items: list[CategorizationApplyOperationHistory]
    page: PageMeta


class CategorizationApplyHistoryResult(ApiModel):
    sequence: int
    transaction_id: uuid.UUID | None
    status: CategorizationApplyStatus
    error_code: str | None
    expected_version: int | None
    current_version: int | None
    created_at: datetime


class CategorizationApplyOperationHistoryDetail(CategorizationApplyOperationHistory):
    results: list[CategorizationApplyHistoryResult]
    page: PageMeta
