import uuid
from datetime import datetime
from typing import Any, Literal

from app.schemas.common import ApiModel, PageMeta


class ImportMappingRequest(ApiModel):
    mapping: dict[str, str]
    locale: Literal["ru-RU", "en-US"] = "ru-RU"


class ImportCommitRequest(ApiModel):
    confirm: bool


class ImportRollbackRequest(ApiModel):
    force: bool = False


class ImportRowOverrideRequest(ApiModel):
    import_as_new: bool


class ImportBatchResponse(ApiModel):
    id: uuid.UUID
    filename: str
    file_type: str
    file_size: int
    file_sha256: str
    status: str
    detected_format: str | None
    mapping: dict[str, Any] | None
    summary: dict[str, Any] | None
    confirmed_at: datetime | None
    rolled_back_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ImportBatchPage(ApiModel):
    items: list[ImportBatchResponse]
    page: PageMeta


class ImportRowResponse(ApiModel):
    id: uuid.UUID
    source_sheet: str | None
    source_row_number: int
    raw_data: dict[str, Any]
    normalized_data: dict[str, Any] | None
    validation_errors: list[dict[str, Any]] | None
    duplicate_transaction_id: uuid.UUID | None
    status: str
    created_transaction_id: uuid.UUID | None


class ImportRowPage(ApiModel):
    items: list[ImportRowResponse]
    page: PageMeta


class ImportActionResponse(ApiModel):
    batch: ImportBatchResponse
    affected_transactions: int = 0
