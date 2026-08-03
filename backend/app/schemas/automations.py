import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, field_validator

from app.schemas.common import ApiModel, PageMeta, require_timezone

ServiceType = Literal["n8n", "backup_agent", "integration"]
ServiceAccountStatus = Literal["active", "revoked", "expired"]
AutomationRunStatus = Literal[
    "started", "completed", "skipped", "waiting_confirmation", "failed", "cancelled"
]

SAFE_SERVICE_PERMISSIONS = frozenset(
    {
        "automation:read",
        "automation:execute",
        "recurring:read",
        "recurring:execute",
        "reports:generate",
        "notifications:send",
        "month-close:prepare",
        "backup:status",
    }
)
FORBIDDEN_SERVICE_PERMISSIONS = frozenset(
    {
        "transactions:arbitrary-write",
        "users:manage",
        "workspace:delete",
        "audit:delete",
        "backup:restore",
    }
)


class ServiceAccountCreate(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    service_type: ServiceType = "n8n"
    permissions: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("permissions")
    @classmethod
    def validate_permissions(cls, value: list[str]) -> list[str]:
        normalized = sorted({item.strip() for item in value if item.strip()})
        invalid = set(normalized) - SAFE_SERVICE_PERMISSIONS
        if invalid:
            raise ValueError(f"Unsupported service permissions: {', '.join(sorted(invalid))}")
        return normalized


class ServiceApiKeyResponse(ApiModel):
    id: uuid.UUID
    key_prefix: str
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None


class ServiceAccountResponse(ApiModel):
    id: uuid.UUID
    workspace_id: uuid.UUID | None
    name: str
    service_type: ServiceType
    status: ServiceAccountStatus
    permissions: list[str]
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None
    keys: list[ServiceApiKeyResponse] = Field(default_factory=list)


class ServiceAccountPage(ApiModel):
    items: list[ServiceAccountResponse]
    page: PageMeta


class ServiceKeyCreate(ApiModel):
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, value: datetime | None) -> datetime | None:
        return require_timezone(value) if value is not None else None


class ServiceKeyOneTimeResponse(ApiModel):
    service_account: ServiceAccountResponse
    key: str
    warning: str


class ServiceAccountActionResponse(ApiModel):
    status: str
    service_account: ServiceAccountResponse


class AutomationRunResponse(ApiModel):
    id: uuid.UUID
    workspace_id: uuid.UUID | None
    automation_type: str
    trigger_type: str
    idempotency_key: str
    status: AutomationRunStatus
    service_account_id: uuid.UUID | None
    initiated_by: uuid.UUID | None
    started_at: datetime
    finished_at: datetime | None
    input_summary: dict[str, Any] | None
    result_summary: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    request_id: uuid.UUID | None
    created_at: datetime


class AutomationRunPage(ApiModel):
    items: list[AutomationRunResponse]
    page: PageMeta


class AutomationHeartbeatRequest(ApiModel):
    n8n_version: str | None = Field(default=None, max_length=50)
    workflow_count: int | None = Field(default=None, ge=0, le=10000)


class AutomationStatusResponse(ApiModel):
    status: Literal["healthy", "stale", "not_configured"]
    last_heartbeat_at: datetime | None
    active_service_account: ServiceAccountResponse | None
    recent_successful_run: AutomationRunResponse | None
    recent_failed_run: AutomationRunResponse | None
    stale_after_minutes: int


class AutomationHeartbeatResponse(ApiModel):
    status: str
    run: AutomationRunResponse
    duplicate: bool


RecurringRuleType = Literal["income", "expense", "transfer"]
RecurringCreationMode = Literal["draft", "confirmed", "reminder_only"]


class RecurringRuleCreate(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    rule_type: RecurringRuleType
    schedule_rrule: str = Field(min_length=1, max_length=1000)
    timezone: str = Field(min_length=1, max_length=100)
    transaction_type: RecurringRuleType
    amount: Decimal = Field(gt=0, max_digits=20, decimal_places=4)
    currency: str = Field(min_length=3, max_length=3)
    account_id: uuid.UUID
    target_account_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    counterparty: str | None = Field(default=None, max_length=300)
    description: str | None = None
    comment: str | None = None
    creation_mode: RecurringCreationMode = "draft"
    days_before_reminder: int = Field(default=0, ge=0, le=365)
    is_active: bool = True


class RecurringRuleUpdate(ApiModel):
    version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    rule_type: RecurringRuleType | None = None
    schedule_rrule: str | None = Field(default=None, min_length=1, max_length=1000)
    timezone: str | None = Field(default=None, min_length=1, max_length=100)
    transaction_type: RecurringRuleType | None = None
    amount: Decimal | None = Field(default=None, gt=0, max_digits=20, decimal_places=4)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    account_id: uuid.UUID | None = None
    target_account_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    counterparty: str | None = Field(default=None, max_length=300)
    description: str | None = None
    comment: str | None = None
    creation_mode: RecurringCreationMode | None = None
    days_before_reminder: int | None = Field(default=None, ge=0, le=365)
    is_active: bool | None = None


class RecurringRuleResponse(ApiModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    rule_type: RecurringRuleType
    schedule_rrule: str
    timezone: str
    transaction_type: RecurringRuleType
    amount: Decimal
    currency: str
    account_id: uuid.UUID
    target_account_id: uuid.UUID | None
    category_id: uuid.UUID | None
    counterparty: str | None
    description: str | None
    comment: str | None
    creation_mode: RecurringCreationMode
    days_before_reminder: int
    is_active: bool
    next_run_at: datetime | None
    last_run_at: datetime | None
    version: int
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class RecurringRulePage(ApiModel):
    items: list[RecurringRuleResponse]
    page: PageMeta


class RecurringExecuteRequest(ApiModel):
    scheduled_for: datetime

    @field_validator("scheduled_for")
    @classmethod
    def validate_scheduled_for(cls, value: datetime) -> datetime:
        return require_timezone(value)


class RecurringRuleExecutionResponse(ApiModel):
    id: uuid.UUID
    rule_id: uuid.UUID
    scheduled_for: datetime
    automation_run_id: uuid.UUID
    transaction_id: uuid.UUID | None
    status: Literal[
        "created",
        "draft_created",
        "confirmed_created",
        "reminder_sent",
        "skipped",
        "failed",
    ]
    created_at: datetime
    completed_at: datetime | None
    duplicate: bool = False


class RecurringRuleHistoryPage(ApiModel):
    items: list[RecurringRuleExecutionResponse]
    page: PageMeta


class RecurringDueItem(ApiModel):
    rule_id: uuid.UUID
    scheduled_for: datetime


class RecurringDueResponse(ApiModel):
    items: list[RecurringDueItem]


class TelegramLinkCodeResponse(ApiModel):
    code: str
    expires_at: datetime
    warning: str


class TelegramLinkRequest(ApiModel):
    code: str = Field(min_length=4, max_length=20)
    telegram_user_id: int
    telegram_chat_id: int
    telegram_username: str | None = Field(default=None, max_length=100)


class TelegramMessageRequest(ApiModel):
    telegram_user_id: int
    telegram_chat_id: int
    text: str = Field(min_length=1, max_length=4000)
    update_id: int | None = None


class TelegramCallbackRequest(ApiModel):
    telegram_user_id: int
    telegram_chat_id: int
    opaque_id: str = Field(min_length=8, max_length=32)
    update_id: int | None = None


class TelegramDeliveryStatusRequest(ApiModel):
    telegram_user_id: int
    telegram_chat_id: int
    delivery_id: str = Field(min_length=1, max_length=200)
    status: Literal["sent", "edited", "failed"]
    error_code: str | None = Field(default=None, max_length=100)


class TelegramButton(ApiModel):
    label: str
    callback_data: str


class TelegramIntegrationResponse(ApiModel):
    status: str
    response_type: Literal[
        "linked",
        "message",
        "preview",
        "ambiguous",
        "confirmed",
        "cancelled",
        "delivery",
    ]
    messages: list[str] = Field(default_factory=list)
    buttons: list[TelegramButton] = Field(default_factory=list)
    intent_id: str | None = None
    transaction_id: uuid.UUID | None = None
    duplicate: bool = False


class TelegramLinkStatusResponse(ApiModel):
    linked: bool
    workspace_id: uuid.UUID | None = None
    telegram_user_id: int | None = None
    telegram_chat_id: int | None = None
    telegram_username: str | None = None
    status: str | None = None
    linked_at: datetime | None = None
    last_seen_at: datetime | None = None


NotificationEventType = Literal[
    "weekly_report",
    "uncategorized_reminder",
    "recurring_due",
    "recurring_created",
    "month_close",
    "backup_problem",
    "sync_problem",
]


class NotificationSettingInput(ApiModel):
    event_type: NotificationEventType
    enabled: bool = True
    schedule_time: str | None = None
    timezone: str = Field(min_length=1, max_length=100)
    configuration: dict[str, Any] | None = None


class NotificationSettingResponse(ApiModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    channel: Literal["telegram", "in_app"]
    event_type: NotificationEventType
    enabled: bool
    schedule_time: time | None
    timezone: str
    configuration: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class NotificationTarget(ApiModel):
    workspace_id: uuid.UUID
    event_type: NotificationEventType
    recipients: list[dict[str, Any]] = Field(default_factory=list)


class NotificationTargetResponse(ApiModel):
    items: list[NotificationTarget]


class PendingNotification(ApiModel):
    opaque_id: str
    telegram_user_id: int
    telegram_chat_id: int
    message: str


class PendingNotificationResponse(ApiModel):
    items: list[PendingNotification]
    duplicate: bool = False


class ReportRequest(ApiModel):
    workspace_id: uuid.UUID


class WeeklyReportRequest(ReportRequest):
    week_start: date


class MoneyTotal(ApiModel):
    currency: str
    amount: Decimal


class WeeklyReportGroup(ApiModel):
    currency: str
    income: Decimal
    expense: Decimal
    net_cashflow: Decimal
    previous_income: Decimal
    previous_expense: Decimal
    top_categories: list[dict[str, Any]]


class ReportRecipient(ApiModel):
    user_id: uuid.UUID
    telegram_user_id: int
    telegram_chat_id: int


class WeeklyReportResponse(ApiModel):
    workspace_id: uuid.UUID
    week_start: date
    week_end: date
    groups: list[WeeklyReportGroup]
    uncategorized_count: int
    draft_count: int
    upcoming_recurring_count: int
    account_balances: list[dict[str, Any]]
    sync_status: str
    backup_status: str
    recipients: list[ReportRecipient]
    messages: list[str]
    duplicate: bool = False


class UncategorizedReportResponse(ApiModel):
    workspace_id: uuid.UUID
    count: int
    totals: list[MoneyTotal]
    latest: list[dict[str, Any]]
    recipients: list[ReportRecipient]
    messages: list[str]
    duplicate: bool = False


class BackupStatusResponse(ApiModel):
    status: Literal["healthy", "stale", "missing", "unverified"]
    last_backup_at: datetime | None
    last_verified_at: datetime | None
    revision: str | None
    age_hours: Decimal | None
    sha256_short: str | None
    stale: bool
    warning: str | None


class MonthCloseConfirmRequest(ApiModel):
    version: int = Field(ge=1)
    confirm: bool


class MonthCloseReopenRequest(ApiModel):
    version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=500)


class MonthClosureResponse(ApiModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    period_month: date
    status: Literal["draft", "ready", "blocked", "confirmed", "reopened"]
    prepared_by: uuid.UUID | None
    confirmed_by: uuid.UUID | None
    prepared_at: datetime | None
    confirmed_at: datetime | None
    summary: dict[str, Any]
    blocking_issues: list[dict[str, Any]] | None
    warning_issues: list[dict[str, Any]] | None
    version: int
    created_at: datetime
    updated_at: datetime


class MonthClosurePage(ApiModel):
    items: list[MonthClosureResponse]
    page: PageMeta
