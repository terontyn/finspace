from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.schemas.common import ApiModel, PageMeta

SyncMode = Literal["push_only", "bidirectional", "paused"]
ConflictResolution = Literal["keep_database", "keep_sheet", "manual_merge"]


class GoogleConnectionStatus(ApiModel):
    configured: bool
    connected: bool
    status: str | None = None
    google_email: str | None = None
    granted_scopes: list[str] = Field(default_factory=list)
    token_expires_at: datetime | None = None


class GoogleConnectResponse(ApiModel):
    authorization_url: str
    expires_at: datetime


class GoogleActionResponse(ApiModel):
    status: str


class GoogleSheetStatus(ApiModel):
    configured: bool
    provider: Literal["apps_script_bridge", "google_oauth"]
    oauth_enabled: bool
    apps_script_bridge_enabled: bool
    public_backend_url: str | None = None
    connection: GoogleConnectionStatus
    binding_id: uuid.UUID | None = None
    spreadsheet_url: str | None = None
    spreadsheet_name: str | None = None
    template_version: int | None = None
    status: str | None = None
    sync_enabled: bool = False
    sync_mode: SyncMode | None = None
    apps_script_enabled: bool = False
    last_successful_sync_at: datetime | None = None
    last_reconciliation_at: datetime | None = None
    pending_outbox: int = 0
    failed_events: int = 0
    open_conflicts: int = 0
    webhook_configured: bool = False
    spreadsheet_registered: bool = False
    last_pull_at: datetime | None = None
    last_ack_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    heartbeat_healthy: bool = False


class GoogleSheetBindingResponse(ApiModel):
    id: uuid.UUID
    provider: str
    spreadsheet_id: str | None
    spreadsheet_url: str | None
    spreadsheet_name: str
    template_version: int
    status: str
    sync_enabled: bool
    sync_mode: SyncMode
    apps_script_enabled: bool
    created_at: datetime


class FullExportRequest(ApiModel):
    force: bool = False


class FullExportPreview(ApiModel):
    transactions: int
    accounts: int
    categories: int
    pending_changes: int
    open_conflicts: int
    blocked: bool
    warning: str


class AppsScriptSecretResponse(ApiModel):
    binding_id: uuid.UUID
    secret: str
    webhook_url: str | None
    secret_version: int
    warning: str


class AppsScriptBindingResponse(ApiModel):
    id: uuid.UUID
    provider: Literal["apps_script_bridge"]
    spreadsheet_id: str | None
    spreadsheet_url: str | None
    spreadsheet_name: str
    template_version: int
    status: str
    sync_enabled: bool
    sync_mode: SyncMode
    secret_created_at: datetime
    secret_last_rotated_at: datetime | None
    last_pull_at: datetime | None
    last_ack_at: datetime | None
    last_heartbeat_at: datetime | None
    created_at: datetime


class AppsScriptBindingCreateResponse(AppsScriptBindingResponse):
    secret: str
    backend_url: str
    warning: str


class AppsScriptRotateSecretRequest(ApiModel):
    rebind: bool = False


class AppsScriptRegisterRequest(ApiModel):
    spreadsheet_id: str = Field(min_length=1, max_length=255)
    spreadsheet_url: str = Field(min_length=1, max_length=2000)
    template_version: int = Field(ge=1)
    apps_script_version: int = Field(ge=1)


class AppsScriptRegisterResponse(ApiModel):
    status: str
    binding_id: uuid.UUID
    spreadsheet_id: str
    initial_export_events: int
    initial_export_run_id: uuid.UUID | None


class AppsScriptPullRequest(ApiModel):
    spreadsheet_id: str = Field(min_length=1, max_length=255)
    limit: int | None = Field(default=None, ge=1, le=500)


class AppsScriptPullEvent(ApiModel):
    event_id: uuid.UUID
    entity_type: Literal["transaction", "account", "category"]
    entity_id: uuid.UUID
    operation: str
    version: int
    sheet_name: str
    row: list[Any]
    row_hash: str
    leased_until: datetime


class AppsScriptPullResponse(ApiModel):
    status: str
    events: list[AppsScriptPullEvent]
    lease_seconds: int


class AppsScriptAckEvent(ApiModel):
    event_id: uuid.UUID
    status: Literal["applied", "failed"]
    row_number: int | None = Field(default=None, ge=2)
    row_hash: str | None = Field(default=None, max_length=64)
    error_code: str | None = Field(default=None, max_length=100)


class AppsScriptAckRequest(ApiModel):
    events: list[AppsScriptAckEvent] = Field(min_length=1, max_length=500)


class AppsScriptAckResponse(ApiModel):
    status: str
    applied: int
    failed: int
    duplicates: int
    initial_export_completed: bool


class AppsScriptPushRequest(ApiModel):
    events: list[WebhookChangeRequest] = Field(min_length=1, max_length=100)


class AppsScriptPushResult(ApiModel):
    event_id: str
    status: str
    result: WebhookChangeResponse | None = None
    error_code: str | None = None
    error_message: str | None = None


class AppsScriptPushResponse(ApiModel):
    results: list[AppsScriptPushResult]


class AppsScriptHeartbeatRequest(ApiModel):
    spreadsheet_id: str = Field(min_length=1, max_length=255)
    apps_script_version: int = Field(ge=1)


class AppsScriptHeartbeatResponse(ApiModel):
    status: str
    server_time: datetime
    binding_status: str
    pending_outbox: int


class AppsScriptSnapshotItem(ApiModel):
    entity_type: Literal["transaction", "account", "category"]
    entity_id: uuid.UUID
    version: int = Field(ge=1)
    row_hash: str = Field(max_length=64)
    row_number: int = Field(ge=2)
    sync_status: str = Field(max_length=40)


class AppsScriptReconcileRequest(ApiModel):
    spreadsheet_id: str = Field(min_length=1, max_length=255)
    snapshot_id: uuid.UUID
    items: list[AppsScriptSnapshotItem] = Field(max_length=1000)
    final: bool = True


class AppsScriptReconcileAction(ApiModel):
    action: Literal["pull", "conflict", "ignore"]
    entity_type: str
    entity_id: uuid.UUID
    row_number: int | None = None
    reason: str


class AppsScriptReconcileResponse(ApiModel):
    status: str
    accepted: int
    run: SyncRunResponse | None = None
    results: dict[str, int] = Field(default_factory=dict)
    actions: list[AppsScriptReconcileAction] = Field(default_factory=list)


class AppsScriptPackageResponse(ApiModel):
    files: dict[str, str]


class WebhookChangeRequest(ApiModel):
    event_id: str = Field(min_length=1, max_length=255)
    spreadsheet_id: str = Field(min_length=1, max_length=255)
    sheet_name: str = Field(min_length=1, max_length=100)
    row_number: int = Field(ge=2)
    entity_type: Literal["transaction", "account", "category"]
    entity_id: uuid.UUID | None = None
    expected_version: int | None = Field(default=None, ge=1)
    row_hash: str | None = Field(default=None, max_length=64)
    changed_fields: dict[str, Any] = Field(default_factory=dict)
    visible_row: dict[str, Any] = Field(default_factory=dict)


class WebhookChangeResponse(ApiModel):
    status: str
    event_id: str
    entity_id: uuid.UUID | None = None
    version: int | None = None
    row_hash: str | None = None
    normalized_row: dict[str, Any] | None = None
    conflict_id: uuid.UUID | None = None


class WebhookPullResponse(ApiModel):
    status: str
    pending: int


class ConflictResponse(ApiModel):
    id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    database_version: int
    sheet_version: int | None
    database_payload: dict[str, Any]
    sheet_payload: dict[str, Any]
    conflicting_fields: list[str]
    status: str
    resolution: str | None
    resolved_payload: dict[str, Any] | None
    created_at: datetime
    resolved_at: datetime | None


class ConflictPage(ApiModel):
    items: list[ConflictResponse]
    page: PageMeta


class ConflictResolveRequest(ApiModel):
    resolution: ConflictResolution
    merged_payload: dict[str, Any] | None = None


class SyncRunResponse(ApiModel):
    id: uuid.UUID
    run_type: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    processed_count: int
    created_count: int
    updated_count: int
    deleted_count: int
    conflict_count: int
    error_count: int
    summary: dict[str, Any] | None


class SyncRunPage(ApiModel):
    items: list[SyncRunResponse]
    page: PageMeta


class ReconciliationResponse(ApiModel):
    run: SyncRunResponse
    results: dict[str, int]
