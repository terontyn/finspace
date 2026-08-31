import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.common import ApiModel, PageMeta
from app.schemas.transactions import TransactionSource, TransactionStatus, TransactionType

MAX_EXPLICIT_IDS = 500
MAX_FILTER_CANDIDATES = 5000

CategorizationPreviewItemStatus = Literal[
    "not_found",
    "transfer",
    "already_categorized",
    "split",
    "reconciled",
    "closed_period",
    "matched",
    "no_match",
]


class SelectionModel(BaseModel):
    """Base for the discriminated selection variants.

    ``ApiModel`` installs a wildcard ``mode="before"`` field validator, and pydantic forbids a
    before-validator on a discriminator field. These variants carry no numeric fields, so the same
    configuration without that guard is equivalent for them.
    """

    model_config = ConfigDict(from_attributes=True, extra="forbid")


class CategorizationPreviewIdsSelection(SelectionModel):
    mode: Literal["ids"]
    transaction_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_EXPLICIT_IDS)

    @model_validator(mode="after")
    def reject_duplicates(self) -> Self:
        # Duplicates are rejected rather than silently deduplicated so the caller's selection count
        # always equals the persisted item count.
        if len(set(self.transaction_ids)) != len(self.transaction_ids):
            raise ValueError("Duplicate transaction identifiers are not allowed")
        return self


class CategorizationPreviewFilterSelection(SelectionModel):
    mode: Literal["filter"]
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None
    account_id: uuid.UUID | None = None
    payee_id: uuid.UUID | None = None
    transaction_type: TransactionType | None = None
    status: TransactionStatus | None = None
    source: TransactionSource | None = None

    @model_validator(mode="after")
    def check_range(self) -> Self:
        if (
            self.occurred_from is not None
            and self.occurred_to is not None
            and self.occurred_from > self.occurred_to
        ):
            raise ValueError("occurred_from must not be after occurred_to")
        return self


CategorizationPreviewSelection = Annotated[
    CategorizationPreviewIdsSelection | CategorizationPreviewFilterSelection,
    Field(discriminator="mode"),
]


class CategorizationPreviewCreate(ApiModel):
    selection: CategorizationPreviewSelection


class CategorizationPreviewSummary(ApiModel):
    selected: int
    matched: int
    no_match: int
    transfer: int
    already_categorized: int
    split: int
    reconciled: int
    closed_period: int
    not_found: int


class CategorizationPreviewResponse(ApiModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    created_by: uuid.UUID
    rule_set_version: int
    selection_mode: Literal["ids", "filter"]
    created_at: datetime
    expires_at: datetime
    summary: CategorizationPreviewSummary


class CategorizationPreviewTransactionSnapshot(ApiModel):
    """Compact immutable review facts, deliberately not a full TransactionResponse."""

    transaction_id: uuid.UUID
    version: int
    occurred_at: datetime
    transaction_type: TransactionType
    amount: Decimal
    currency: str
    account_id: uuid.UUID
    payee_id: uuid.UUID | None
    counterparty: str | None
    description: str | None
    status: TransactionStatus
    source: TransactionSource


class CategorizationPreviewItemResponse(ApiModel):
    id: uuid.UUID
    sequence: int
    transaction_id: uuid.UUID
    transaction_version: int | None
    status: CategorizationPreviewItemStatus
    transaction: CategorizationPreviewTransactionSnapshot | None
    rule_id: uuid.UUID | None
    rule_version: int | None
    rule_name: str | None
    category_id: uuid.UUID | None
    category_version: int | None
    category_name: str | None


class CategorizationPreviewItemPage(ApiModel):
    items: list[CategorizationPreviewItemResponse]
    page: PageMeta
