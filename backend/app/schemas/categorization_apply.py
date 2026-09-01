import uuid
from typing import Literal, Self

from pydantic import Field, model_validator

from app.schemas.common import ApiModel

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
