import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models.account_reconciliation import AccountReconciliation
from app.db.models.accounts import Account
from app.db.models.audit import AuditLog
from app.db.models.automations import (
    MonthCloseControl,
    MonthCloseRevision,
    MonthClosure,
    RecurringRule,
    RecurringRuleExecution,
)
from app.db.models.google_sync import SyncConflict, SyncOutbox
from app.db.models.imports import ImportBatch, ImportRow
from app.db.models.transactions import FinancialTransaction
from app.db.models.users import User, Workspace
from app.dependencies.context import RequestContext
from app.schemas.accounts import AccountBalance
from app.schemas.common import Money
from app.schemas.financial_reports import FinancialReportCategory, FinancialReportGroup
from app.services.audit import record_audit, request_uuid
from app.services.backup_status import get_backup_status
from app.services.budgets import planning_snapshot_for_close
from app.services.calculations import calculate_balances
from app.services.financial_period_guard import (
    get_or_create_control,
    month_end,
    next_month,
    period_bounds,
    previous_month,
)
from app.services.month_close_fingerprint import financial_fingerprint, hash_canonical
from app.services.reports import financial_report

EFFECTIVE_STATUSES = ("confirmed", "reconciled")
MONEY_ADAPTER = TypeAdapter(Money)

ISSUE_DEFINITIONS: dict[str, tuple[str, str, str]] = {
    "MONTH_CLOSE_PERIOD_NOT_ENDED": (
        "blocker",
        "period",
        "The closing month has not ended yet.",
    ),
    "DRAFT_TRANSACTIONS": (
        "blocker",
        "closing_interval",
        "The closing interval contains draft transactions.",
    ),
    "SYNC_CONFLICTS_IN_PERIOD": (
        "blocker",
        "closing_interval",
        "Open sync conflicts may change the closing interval.",
    ),
    "MONTH_CLOSE_SEQUENCE_CONFLICT": (
        "blocker",
        "sequence",
        "Months must be closed sequentially.",
    ),
    "UNCATEGORIZED_TRANSACTIONS": (
        "warning",
        "period",
        "Some income or expense transactions are uncategorized.",
    ),
    "POSSIBLE_DUPLICATES": (
        "warning",
        "period",
        "Transactions with identical key attributes were found.",
    ),
    "NEGATIVE_PERIOD_END_BALANCES": (
        "warning",
        "period_end_balances",
        "Non-credit accounts have negative period-end balances.",
    ),
    "ACCOUNT_NOT_RECONCILED": (
        "warning",
        "reconciliation",
        "Not all material accounts have reconciliation evidence through period end.",
    ),
    "FAILED_RECURRING_EXECUTIONS": (
        "warning",
        "period",
        "Recurring transaction executions failed during the period.",
    ),
    "FAILED_SYNC_OUTBOX": (
        "warning",
        "workspace_delivery",
        "The sync delivery queue contains failed events.",
    ),
    "IMPORT_ROWS_REQUIRING_ATTENTION": (
        "warning",
        "import_staging",
        "Import staging contains rows that require attention.",
    ),
    "STAGED_IMPORTS": (
        "info",
        "import_staging",
        "Unfinished import batches are not yet part of the ledger.",
    ),
    "OUT_OF_PERIOD_SYNC_CONFLICTS": (
        "info",
        "outside_closing_interval",
        "Open sync conflicts do not affect the closing interval.",
    ),
    "NO_FINANCIAL_ACTIVITY": (
        "info",
        "period",
        "The month has no effective financial activity.",
    ),
}


def _issue(
    code: str,
    *,
    count: int = 1,
    details: dict[str, object] | None = None,
    severity: str | None = None,
    scope: str | None = None,
    message: str | None = None,
    **compatibility: object,
) -> dict[str, object]:
    default_severity, default_scope, default_message = ISSUE_DEFINITIONS.get(
        code,
        (
            "blocker" if code.startswith("BACKUP_") else "warning",
            "backup" if code.startswith("BACKUP_") else "workspace",
            "The condition requires attention before month close.",
        ),
    )
    return {
        "code": code,
        "severity": severity or default_severity,
        "scope": scope or default_scope,
        "count": count,
        "message": message or default_message,
        "details": details or {},
        **compatibility,
    }


def period_date(year: int, month: int) -> date:
    if year < 2000 or year > 2200 or month < 1 or month > 12:
        raise ApiError(
            status_code=422,
            code="MONTH_CLOSE_PERIOD_INVALID",
            message="Month close period is invalid",
        )
    return date(year, month, 1)


def _validate_completed_period(workspace: Workspace, period: date) -> None:
    _, end = period_bounds(period, workspace.timezone)
    if end > datetime.now(UTC):
        raise ApiError(
            status_code=409,
            code="MONTH_CLOSE_PERIOD_NOT_ENDED",
            message="The current or a future month cannot be closed",
            details={
                "period": period.strftime("%Y-%m"),
                "issue": _issue(
                    "MONTH_CLOSE_PERIOD_NOT_ENDED",
                    details={"period": period.strftime("%Y-%m")},
                ),
            },
        )


async def get_closure(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    period: date,
    *,
    for_update: bool = False,
) -> MonthClosure:
    statement = select(MonthClosure).where(
        MonthClosure.workspace_id == workspace_id,
        MonthClosure.period_month == period,
    )
    if for_update:
        statement = statement.with_for_update()
    closure = await session.scalar(statement)
    if closure is None:
        raise ApiError(
            status_code=404,
            code="MONTH_CLOSE_NOT_FOUND",
            message="Month close was not prepared",
        )
    return closure


async def _get_or_create_closure(
    session: AsyncSession, workspace_id: uuid.UUID, period: date
) -> MonthClosure:
    await session.execute(
        insert(MonthClosure)
        .values(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            period_month=period,
            status="draft",
            summary={},
            version=1,
        )
        .on_conflict_do_nothing(
            index_elements=[MonthClosure.workspace_id, MonthClosure.period_month]
        )
    )
    return await get_closure(session, workspace_id, period, for_update=True)


async def list_closures(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> tuple[list[MonthClosure], int]:
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(MonthClosure)
            .where(MonthClosure.workspace_id == workspace_id)
        )
        or 0
    )
    items = list(
        (
            await session.scalars(
                select(MonthClosure)
                .where(MonthClosure.workspace_id == workspace_id)
                .order_by(MonthClosure.period_month.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return items, total


async def read_control(session: AsyncSession, workspace_id: uuid.UUID) -> MonthCloseControl:
    control = await session.get(MonthCloseControl, workspace_id)
    if control is not None:
        return control
    return MonthCloseControl(
        workspace_id=workspace_id,
        closed_through=None,
        backup_policy="warn",
        version=1,
    )


def capabilities(
    *,
    role: str,
    workspace: Workspace,
    period: date,
    status: str,
    control: MonthCloseControl,
) -> dict[str, bool]:
    _, end = period_bounds(period, workspace.timezone)
    completed = end <= datetime.now(UTC)
    can_edit = role in {"editor", "owner"}
    return {
        "can_prepare": can_edit and completed and status != "confirmed",
        "can_confirm": role == "owner" and status == "ready",
        "can_reopen": (
            role == "owner"
            and status == "confirmed"
            and control.closed_through == month_end(period)
        ),
        "can_view_history": True,
    }


async def revision_numbers(
    session: AsyncSession, closure_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    if not closure_ids:
        return {}
    rows = (
        await session.execute(
            select(
                MonthCloseRevision.closure_id,
                func.max(MonthCloseRevision.revision_number),
            )
            .where(MonthCloseRevision.closure_id.in_(closure_ids))
            .group_by(MonthCloseRevision.closure_id)
        )
    ).all()
    return {closure_id: int(number) for closure_id, number in rows}


async def current_revision_number(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    revision_id: uuid.UUID | None,
) -> int | None:
    if revision_id is None:
        return None
    number = await session.scalar(
        select(MonthCloseRevision.revision_number).where(
            MonthCloseRevision.id == revision_id,
            MonthCloseRevision.workspace_id == workspace_id,
        )
    )
    return int(number) if number is not None else None


async def list_period_summaries(
    session: AsyncSession,
    workspace: Workspace,
    role: str,
    closures: list[MonthClosure],
    control: MonthCloseControl,
) -> list[dict[str, object]]:
    by_period = {item.period_month: item for item in closures}
    current = datetime.now(ZoneInfo(workspace.timezone)).date().replace(day=1)
    periods = set(by_period)
    periods.update(date(current.year, month, 1) for month in range(1, current.month + 1))
    numbers = await revision_numbers(session, [item.id for item in closures])
    result: list[dict[str, object]] = []
    for period in sorted(periods, reverse=True)[:120]:
        closure = by_period.get(period)
        status = closure.status if closure is not None else "not_prepared"
        result.append(
            {
                "period_month": period,
                "status": status,
                "version": closure.version if closure is not None else None,
                "current_revision": numbers.get(closure.id) if closure is not None else None,
                "prepared": closure is not None and closure.prepared_at is not None,
                "blocker_count": len(closure.blocking_issues or []) if closure else 0,
                "warning_count": len(closure.warning_issues or []) if closure else 0,
                "confirmed_at": closure.confirmed_at if closure is not None else None,
                "reopened_at": closure.last_reopened_at if closure is not None else None,
                "capabilities": capabilities(
                    role=role,
                    workspace=workspace,
                    period=period,
                    status=status,
                    control=control,
                ),
            }
        )
    return result


def _expected_next_period(control: MonthCloseControl) -> date | None:
    if control.closed_through is None:
        return None
    return next_month(date(control.closed_through.year, control.closed_through.month, 1))


def _sequence_issue(control: MonthCloseControl, period: date) -> dict[str, object] | None:
    expected = _expected_next_period(control)
    if expected is None or period == expected:
        return None
    details: dict[str, object] = {
        "expected_period": expected.strftime("%Y-%m"),
        "requested_period": period.strftime("%Y-%m"),
    }
    return _issue(
        "MONTH_CLOSE_SEQUENCE_CONFLICT",
        details=details,
        expected_period=details["expected_period"],
        requested_period=details["requested_period"],
    )


async def _sequence_issue_for_state(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    control: MonthCloseControl,
    period: date,
) -> dict[str, object] | None:
    issue = _sequence_issue(control, period)
    if issue is not None or control.closed_through is not None:
        return issue
    legacy_confirmed = int(
        await session.scalar(
            select(func.count())
            .select_from(MonthClosure)
            .where(
                MonthClosure.workspace_id == workspace_id,
                MonthClosure.status == "confirmed",
            )
        )
        or 0
    )
    if not legacy_confirmed:
        return None
    details: dict[str, object] = {
        "requested_period": period.strftime("%Y-%m"),
        "reason": "legacy_confirmed_history_is_ambiguous",
    }
    return _issue(
        "MONTH_CLOSE_SEQUENCE_CONFLICT",
        details=details,
        requested_period=details["requested_period"],
        reason=details["reason"],
    )


def _normalized_issues(items: list[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(items, key=hash_canonical)


def _prepare_token(
    closure: MonthClosure,
    *,
    generation: int,
    financial_hash: str,
    budget_plan_hash: str,
    summary: dict[str, object],
    blocking: list[dict[str, object]],
    warnings: list[dict[str, object]],
    infos: list[dict[str, object]],
) -> str:
    return hash_canonical(
        {
            "workspace_id": str(closure.workspace_id),
            "closure_id": str(closure.id),
            "generation": generation,
            "period": closure.period_month.isoformat(),
            "financial_fingerprint": financial_hash,
            "budget_plan_fingerprint": budget_plan_hash,
            "summary": summary,
            "blocking": _normalized_issues(blocking),
            "warnings": _normalized_issues(warnings),
            "infos": _normalized_issues(infos),
        }
    )


async def prepare(
    session: AsyncSession,
    workspace: Workspace,
    period: date,
    *,
    actor_user_id: uuid.UUID | None,
    request_id: str,
    source: str,
) -> MonthClosure:
    _validate_completed_period(workspace, period)
    control = await get_or_create_control(session, workspace.id, for_update=True)
    closure = await _get_or_create_closure(session, workspace.id, period)
    if closure.status == "confirmed":
        raise ApiError(
            status_code=409,
            code="MONTH_ALREADY_CLOSED",
            message="Month is already closed",
        )
    (
        summary,
        blocking,
        warnings,
        infos,
        fingerprint,
        _,
        budget_plan_fingerprint,
    ) = await collect_preview(session, workspace, period, control)
    generation = closure.version + 1
    token = _prepare_token(
        closure,
        generation=generation,
        financial_hash=fingerprint,
        budget_plan_hash=budget_plan_fingerprint,
        summary=summary,
        blocking=blocking,
        warnings=warnings,
        infos=infos,
    )
    before = {"status": closure.status, "version": closure.version}
    now = datetime.now(UTC)
    closure.status = "blocked" if blocking else "ready"
    closure.prepared_by = actor_user_id
    closure.prepared_at = now
    closure.summary = {**summary, "info_issues": infos}
    closure.blocking_issues = blocking
    closure.warning_issues = warnings
    closure.prepare_token = token
    closure.prepared_fingerprint = fingerprint
    closure.version = generation
    closure.updated_at = now
    await session.flush()
    await record_audit(
        session,
        workspace_id=workspace.id,
        actor_user_id=actor_user_id,
        entity_type="month_closure",
        entity_id=closure.id,
        action="month_close.prepare",
        before_data=before,
        after_data={
            "period": period.isoformat(),
            "status": closure.status,
            "version": closure.version,
            "financial_fingerprint": fingerprint,
            "budget_plan_fingerprint": budget_plan_fingerprint,
            "blocking_count": len(blocking),
            "warning_count": len(warnings),
            "info_count": len(infos),
        },
        request_id=request_id,
        source=source,
    )
    await session.commit()
    await session.refresh(closure)
    return closure


def _validate_idempotency_key(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 255:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="X-Idempotency-Key is required and must contain at most 255 characters",
        )
    return normalized


def _idempotency_hash(action: str, closure: MonthClosure, payload: dict[str, object]) -> str:
    return hash_canonical(
        {
            "action": action,
            "workspace_id": str(closure.workspace_id),
            "closure_id": str(closure.id),
            "payload": payload,
        }
    )


async def _revision_for_key(
    session: AsyncSession, workspace_id: uuid.UUID, idempotency_key: str
) -> MonthCloseRevision | None:
    return await session.scalar(
        select(MonthCloseRevision).where(
            MonthCloseRevision.workspace_id == workspace_id,
            MonthCloseRevision.idempotency_key == idempotency_key,
        )
    )


async def _reopen_audit_for_key(
    session: AsyncSession, workspace_id: uuid.UUID, idempotency_key: str
) -> AuditLog | None:
    return await session.scalar(
        select(AuditLog).where(
            AuditLog.workspace_id == workspace_id,
            AuditLog.action == "month_close.reopen",
            AuditLog.after_data["idempotency_key"].astext == idempotency_key,
        )
    )


async def _reopen_audit_period(session: AsyncSession, audit: AuditLog) -> date | None:
    response = dict(audit.after_data or {}).get("response")
    if isinstance(response, dict) and response.get("period_month") is not None:
        try:
            return date.fromisoformat(str(response["period_month"]))
        except ValueError:
            return None
    return await session.scalar(
        select(MonthClosure.period_month).where(
            MonthClosure.id == audit.entity_id,
            MonthClosure.workspace_id == audit.workspace_id,
        )
    )


def _raise_idempotency_conflict() -> None:
    raise ApiError(
        status_code=409,
        code="MONTH_CLOSE_IDEMPOTENCY_CONFLICT",
        message="Idempotency key was already used for another month close request",
    )


def _closure_response_snapshot(closure: MonthClosure) -> dict[str, object]:
    """Persist the exact successful API result without financial payload expansion."""
    return {
        "id": str(closure.id),
        "workspace_id": str(closure.workspace_id),
        "period_month": closure.period_month.isoformat(),
        "status": closure.status,
        "prepared_by": str(closure.prepared_by) if closure.prepared_by else None,
        "confirmed_by": str(closure.confirmed_by) if closure.confirmed_by else None,
        "prepared_at": closure.prepared_at.isoformat() if closure.prepared_at else None,
        "confirmed_at": closure.confirmed_at.isoformat() if closure.confirmed_at else None,
        "summary": closure.summary,
        "blocking_issues": closure.blocking_issues,
        "warning_issues": closure.warning_issues,
        "prepare_token": closure.prepare_token,
        "prepared_fingerprint": closure.prepared_fingerprint,
        "current_revision_id": (
            str(closure.current_revision_id) if closure.current_revision_id else None
        ),
        "last_reopened_at": (
            closure.last_reopened_at.isoformat() if closure.last_reopened_at else None
        ),
        "last_reopened_by": (str(closure.last_reopened_by) if closure.last_reopened_by else None),
        "last_reopen_reason": closure.last_reopen_reason,
        "version": closure.version,
        "created_at": closure.created_at.isoformat(),
        "updated_at": closure.updated_at.isoformat(),
    }


def _closure_from_response_snapshot(data: dict[str, Any]) -> MonthClosure:
    """Build a detached result for an idempotent replay of a successful request."""

    def optional_uuid(value: object) -> uuid.UUID | None:
        return uuid.UUID(str(value)) if value is not None else None

    def optional_datetime(value: object) -> datetime | None:
        return datetime.fromisoformat(str(value)) if value is not None else None

    return MonthClosure(
        id=uuid.UUID(str(data["id"])),
        workspace_id=uuid.UUID(str(data["workspace_id"])),
        period_month=date.fromisoformat(str(data["period_month"])),
        status=str(data["status"]),
        prepared_by=optional_uuid(data.get("prepared_by")),
        confirmed_by=optional_uuid(data.get("confirmed_by")),
        prepared_at=optional_datetime(data.get("prepared_at")),
        confirmed_at=optional_datetime(data.get("confirmed_at")),
        summary=dict(data.get("summary") or {}),
        blocking_issues=data.get("blocking_issues"),
        warning_issues=data.get("warning_issues"),
        prepare_token=data.get("prepare_token"),
        prepared_fingerprint=data.get("prepared_fingerprint"),
        current_revision_id=optional_uuid(data.get("current_revision_id")),
        last_reopened_at=optional_datetime(data.get("last_reopened_at")),
        last_reopened_by=optional_uuid(data.get("last_reopened_by")),
        last_reopen_reason=data.get("last_reopen_reason"),
        version=int(data["version"]),
        created_at=datetime.fromisoformat(str(data["created_at"])),
        updated_at=datetime.fromisoformat(str(data["updated_at"])),
    )


async def confirm(
    session: AsyncSession,
    context: RequestContext,
    period: date,
    *,
    version: int,
    explicit: bool,
    prepare_token: str,
    idempotency_key: str,
) -> MonthClosure:
    key = _validate_idempotency_key(idempotency_key)
    control = await get_or_create_control(session, context.workspace.id, for_update=True)
    existing = await _revision_for_key(session, context.workspace.id, key)
    if existing is not None and existing.period_month != period:
        _raise_idempotency_conflict()
    if await _reopen_audit_for_key(session, context.workspace.id, key) is not None:
        _raise_idempotency_conflict()
    _validate_completed_period(context.workspace, period)
    closure = await get_closure(session, context.workspace.id, period, for_update=True)
    semantic_hash = _idempotency_hash(
        "confirm",
        closure,
        {"version": version, "confirm": explicit, "prepare_token": prepare_token},
    )
    if existing is not None:
        idempotency = dict(existing.snapshot.get("_idempotency", {}))
        stored = idempotency.get("payload_hash")
        if existing.closure_id != closure.id or stored != semantic_hash:
            _raise_idempotency_conflict()
        response = idempotency.get("response")
        if isinstance(response, dict):
            return _closure_from_response_snapshot(response)
        return closure
    if not explicit:
        raise ApiError(
            status_code=409,
            code="MONTH_CLOSE_INVALID_STATE",
            message="Explicit month close confirmation is required",
        )
    if closure.status == "confirmed":
        raise ApiError(
            status_code=409,
            code="MONTH_ALREADY_CLOSED",
            message="Month is already closed",
        )
    if closure.status != "ready":
        raise ApiError(
            status_code=409,
            code="MONTH_CLOSE_INVALID_STATE",
            message="Only a freshly prepared ready month can be confirmed",
            details={"current_status": closure.status},
        )
    if closure.version != version:
        raise ApiError(
            status_code=409,
            code="MONTH_CLOSE_VERSION_CONFLICT",
            message="Month close version is stale",
            details={"current_version": closure.version},
        )
    sequence = await _sequence_issue_for_state(session, context.workspace.id, control, period)
    if sequence is not None:
        raise ApiError(
            status_code=409,
            code="MONTH_CLOSE_SEQUENCE_CONFLICT",
            message="Months must be closed sequentially",
            details=sequence,
        )
    (
        summary,
        blocking,
        warnings,
        infos,
        fingerprint,
        _,
        budget_plan_fingerprint,
    ) = await collect_preview(session, context.workspace, period, control)
    actual_token = _prepare_token(
        closure,
        generation=closure.version,
        financial_hash=fingerprint,
        budget_plan_hash=budget_plan_fingerprint,
        summary=summary,
        blocking=blocking,
        warnings=warnings,
        infos=infos,
    )
    if (
        prepare_token != closure.prepare_token
        or fingerprint != closure.prepared_fingerprint
        or actual_token != closure.prepare_token
    ):
        raise ApiError(
            status_code=409,
            code="MONTH_CLOSE_PREVIEW_STALE",
            message="Month close preview changed; prepare the month again",
        )
    if blocking:
        raise ApiError(
            status_code=409,
            code="MONTH_CLOSE_BLOCKED",
            message="Month close has blocking issues",
            details={"issues": blocking},
        )

    revision_number = (
        int(
            await session.scalar(
                select(func.coalesce(func.max(MonthCloseRevision.revision_number), 0)).where(
                    MonthCloseRevision.closure_id == closure.id
                )
            )
            or 0
        )
        + 1
    )
    now = datetime.now(UTC)
    start, end = period_bounds(period, context.workspace.timezone)
    snapshot = {
        **summary,
        "period": {
            "month": period.isoformat(),
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
            "timezone": context.workspace.timezone,
        },
        "issues": {"blocking": blocking, "warnings": warnings, "info": infos},
        "confirmation": {
            "confirmed_by": str(context.user.id),
            "confirmed_at": now.isoformat(),
            "revision_number": revision_number,
        },
        "financial_fingerprint": {
            "algorithm": "sha256",
            "value": fingerprint,
            "confirmed_and_reconciled_equivalent": True,
        },
        "_idempotency": {"payload_hash": semantic_hash},
    }
    revision = MonthCloseRevision(
        workspace_id=context.workspace.id,
        closure_id=closure.id,
        revision_number=revision_number,
        period_month=period,
        period_start_at=start,
        period_end_at=end,
        snapshot=snapshot,
        financial_fingerprint=fingerprint,
        legacy_unverified=False,
        confirmed_by=context.user.id,
        confirmed_at=now,
        request_id=request_uuid(context.request_id),
        source="api",
        idempotency_key=key,
        created_at=now,
    )
    session.add(revision)
    await session.flush()
    before = {"status": closure.status, "version": closure.version}
    closure.status = "confirmed"
    closure.confirmed_by = context.user.id
    closure.confirmed_at = now
    closure.summary = {**summary, "info_issues": infos}
    closure.blocking_issues = []
    closure.warning_issues = warnings
    closure.current_revision_id = revision.id
    closure.version += 1
    closure.updated_at = now
    control.closed_through = month_end(period)
    control.version += 1
    control.updated_at = now
    revision.snapshot = {
        **snapshot,
        "_idempotency": {
            "payload_hash": semantic_hash,
            "response": _closure_response_snapshot(closure),
        },
    }
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="month_closure",
        entity_id=closure.id,
        action="month_close.confirm",
        before_data=before,
        after_data={
            "period": period.isoformat(),
            "status": "confirmed",
            "revision_id": str(revision.id),
            "revision_number": revision_number,
            "financial_fingerprint": fingerprint,
            "budget_plan_fingerprint": budget_plan_fingerprint,
            "idempotency_key": key,
        },
        request_id=context.request_id,
    )
    await session.commit()
    await session.refresh(closure)
    return closure


async def reopen(
    session: AsyncSession,
    context: RequestContext,
    period: date,
    *,
    version: int,
    reason: str,
    idempotency_key: str,
) -> MonthClosure:
    key = _validate_idempotency_key(idempotency_key)
    control = await get_or_create_control(session, context.workspace.id, for_update=True)
    if await _revision_for_key(session, context.workspace.id, key) is not None:
        _raise_idempotency_conflict()
    existing_audit = await _reopen_audit_for_key(session, context.workspace.id, key)
    if existing_audit is not None:
        if await _reopen_audit_period(session, existing_audit) != period:
            _raise_idempotency_conflict()
    closure = await get_closure(session, context.workspace.id, period, for_update=True)
    normalized_reason = reason.strip()
    semantic_hash = _idempotency_hash(
        "reopen", closure, {"version": version, "reason": normalized_reason}
    )
    if existing_audit is not None:
        stored = dict(existing_audit.after_data or {}).get("payload_hash")
        if existing_audit.entity_id != closure.id or stored != semantic_hash:
            _raise_idempotency_conflict()
        response = dict(existing_audit.after_data or {}).get("response")
        if isinstance(response, dict):
            return _closure_from_response_snapshot(response)
        return closure
    if closure.version != version:
        raise ApiError(
            status_code=409,
            code="MONTH_CLOSE_VERSION_CONFLICT",
            message="Month close version is stale",
            details={"current_version": closure.version},
        )
    if closure.status != "confirmed":
        raise ApiError(
            status_code=409,
            code="MONTH_CLOSE_INVALID_STATE",
            message="Only a confirmed month can be reopened",
            details={"current_status": closure.status},
        )
    if control.closed_through != month_end(period):
        raise ApiError(
            status_code=409,
            code="MONTH_CLOSE_REOPEN_ORDER_CONFLICT",
            message="Only the latest confirmed month can be reopened",
            details={
                "closed_through": (
                    control.closed_through.isoformat() if control.closed_through else None
                )
            },
        )
    previous = previous_month(period)
    previous_closure = await session.scalar(
        select(MonthClosure).where(
            MonthClosure.workspace_id == context.workspace.id,
            MonthClosure.period_month == previous,
            MonthClosure.status == "confirmed",
        )
    )
    now = datetime.now(UTC)
    before = {
        "status": closure.status,
        "version": closure.version,
        "closed_through": control.closed_through.isoformat(),
    }
    closure.status = "reopened"
    closure.last_reopened_at = now
    closure.last_reopened_by = context.user.id
    closure.last_reopen_reason = normalized_reason
    closure.version += 1
    closure.updated_at = now
    control.closed_through = month_end(previous) if previous_closure is not None else None
    control.version += 1
    control.updated_at = now
    await record_audit(
        session,
        workspace_id=closure.workspace_id,
        actor_user_id=context.user.id,
        entity_type="month_closure",
        entity_id=closure.id,
        action="month_close.reopen",
        before_data=before,
        after_data={
            "status": "reopened",
            "reason": normalized_reason,
            "closed_through": (
                control.closed_through.isoformat() if control.closed_through else None
            ),
            "current_revision_id": (
                str(closure.current_revision_id) if closure.current_revision_id else None
            ),
            "idempotency_key": key,
            "payload_hash": semantic_hash,
            "response": _closure_response_snapshot(closure),
        },
        request_id=context.request_id,
    )
    await session.commit()
    await session.refresh(closure)
    return closure


async def collect_preview(
    session: AsyncSession,
    workspace: Workspace,
    period: date,
    control: MonthCloseControl,
) -> tuple[
    dict[str, object],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    str,
    dict[str, object],
    str,
]:
    start, end = period_bounds(period, workspace.timezone)
    closing_start = None
    if control.closed_through is not None:
        closing_start, _ = period_bounds(
            next_month(date(control.closed_through.year, control.closed_through.month, 1)),
            workspace.timezone,
        )
    period_transactions = list(
        (
            await session.scalars(
                select(FinancialTransaction).where(
                    FinancialTransaction.workspace_id == workspace.id,
                    FinancialTransaction.occurred_at >= start,
                    FinancialTransaction.occurred_at < end,
                    FinancialTransaction.deleted_at.is_(None),
                )
            )
        ).all()
    )
    closing_filters = [
        FinancialTransaction.workspace_id == workspace.id,
        FinancialTransaction.occurred_at < end,
        FinancialTransaction.deleted_at.is_(None),
    ]
    if closing_start is not None:
        closing_filters.append(FinancialTransaction.occurred_at >= closing_start)
    closing_transactions = list(
        (await session.scalars(select(FinancialTransaction).where(*closing_filters))).all()
    )
    blocking: list[dict[str, object]] = []
    warnings: list[dict[str, object]] = []
    infos: list[dict[str, object]] = []
    sequence = await _sequence_issue_for_state(session, workspace.id, control, period)
    if sequence is not None:
        blocking.append(sequence)
    draft_count = sum(item.status == "draft" for item in closing_transactions)
    if draft_count:
        blocking.append(_issue("DRAFT_TRANSACTIONS", count=draft_count))

    uncategorized = sum(
        item.status != "cancelled"
        and item.transaction_type in {"income", "expense"}
        and item.category_id is None
        for item in period_transactions
    )
    if uncategorized:
        warnings.append(_issue("UNCATEGORIZED_TRANSACTIONS", count=uncategorized))

    in_period_conflicts, out_of_period_conflicts = await _sync_conflict_counts(
        session, workspace, closing_start, end
    )
    if in_period_conflicts:
        blocking.append(_issue("SYNC_CONFLICTS_IN_PERIOD", count=in_period_conflicts))
    if out_of_period_conflicts:
        infos.append(_issue("OUT_OF_PERIOD_SYNC_CONFLICTS", count=out_of_period_conflicts))

    failed_outbox = await _count(
        session,
        select(SyncOutbox.id).where(
            SyncOutbox.workspace_id == workspace.id,
            SyncOutbox.status == "failed",
        ),
    )
    if failed_outbox:
        warnings.append(_issue("FAILED_SYNC_OUTBOX", count=failed_outbox))

    invalid_import_rows = await _count(
        session,
        select(ImportRow.id)
        .join(ImportBatch, ImportBatch.id == ImportRow.batch_id)
        .where(
            ImportBatch.workspace_id == workspace.id,
            ImportRow.status == "invalid",
        ),
    )
    if invalid_import_rows:
        warnings.append(_issue("IMPORT_ROWS_REQUIRING_ATTENTION", count=invalid_import_rows))
    staged_imports = await _count(
        session,
        select(ImportBatch.id).where(
            ImportBatch.workspace_id == workspace.id,
            ImportBatch.status.in_(
                ("uploaded", "parsed", "mapping_required", "validated", "ready", "importing")
            ),
        ),
    )
    if staged_imports:
        infos.append(_issue("STAGED_IMPORTS", count=staged_imports))

    failed_recurring = await _count(
        session,
        select(RecurringRuleExecution.id)
        .join(RecurringRule, RecurringRule.id == RecurringRuleExecution.rule_id)
        .where(
            RecurringRule.workspace_id == workspace.id,
            RecurringRuleExecution.status == "failed",
            RecurringRuleExecution.scheduled_for >= start,
            RecurringRuleExecution.scheduled_for < end,
        ),
    )
    if failed_recurring:
        warnings.append(_issue("FAILED_RECURRING_EXECUTIONS", count=failed_recurring))

    backup = await get_backup_status(session)
    if backup.status != "healthy":
        backup_code = f"BACKUP_{backup.status.upper()}"
        backup_issue = _issue(
            backup_code,
            severity="blocker" if control.backup_policy == "require_healthy" else "warning",
            scope="backup",
            message={
                "missing": "No backup has been registered.",
                "unverified": "The latest backup has not been restore-verified.",
                "stale": "The latest verified backup is stale.",
            }.get(backup.status, "Backup state requires attention."),
            details={"status": backup.status},
            status=backup.status,
        )
        if control.backup_policy == "require_healthy":
            blocking.append(backup_issue)
        else:
            warnings.append(backup_issue)

    duplicate_keys = Counter(
        (
            item.occurred_at.replace(microsecond=0),
            item.transaction_type,
            item.amount,
            item.currency,
            item.account_id,
        )
        for item in period_transactions
        if item.status != "cancelled"
    )
    duplicate_count = sum(count - 1 for count in duplicate_keys.values() if count > 1)
    if duplicate_count:
        warnings.append(_issue("POSSIBLE_DUPLICATES", count=duplicate_count))

    balances = await calculate_balances(session, workspace.id, as_of=end)
    account_types = {
        item.id: item.account_type
        for item in (
            list(
                (
                    await session.scalars(
                        select(Account).where(
                            Account.workspace_id == workspace.id,
                            Account.id.in_([balance.account_id for balance in balances]),
                        )
                    )
                ).all()
            )
            if balances
            else []
        )
    }
    negative = [
        item
        for item in balances
        if item.balance < Decimal("0") and account_types.get(item.account_id) != "credit_card"
    ]
    if negative:
        warnings.append(
            _issue(
                "NEGATIVE_PERIOD_END_BALANCES",
                count=len(negative),
                details={"account_ids": [str(item.account_id) for item in negative]},
            )
        )
    reconciliation_coverage = await _reconciliation_coverage(
        session, workspace.id, period, balances, period_transactions
    )
    unreconciled = [item for item in reconciliation_coverage if item["state"] == "not_reconciled"]
    if unreconciled:
        warnings.append(
            _issue(
                "ACCOUNT_NOT_RECONCILED",
                count=len(unreconciled),
                details={"account_ids": [str(item["account_id"]) for item in unreconciled]},
            )
        )

    report = await financial_report(
        session,
        workspace,
        date_from=period,
        date_to=month_end(period),
        currency=None,
    )
    currencies = [item.model_dump(mode="json") for item in report.groups]
    effective_count = sum(int(item["transactions_count"]) for item in currencies)
    if effective_count == 0:
        infos.append(_issue("NO_FINANCIAL_ACTIVITY", count=0))
    fingerprint = await financial_fingerprint(session, workspace.id, end)
    planning_budget, budget_plan_fingerprint = await planning_snapshot_for_close(
        session, workspace, period, control
    )
    summary: dict[str, object] = {
        "transaction_count": effective_count,
        "draft_count": draft_count,
        "uncategorized_count": uncategorized,
        "possible_duplicate_count": duplicate_count,
        "negative_balance_count": len(negative),
        "backup_status": backup.status,
        "backup_policy": control.backup_policy,
        "backup_verified_at": (
            backup.last_verified_at.isoformat() if backup.last_verified_at else None
        ),
        "currencies": currencies,
        "account_balances": [item.model_dump(mode="json") for item in balances],
        "reconciliation_coverage": reconciliation_coverage,
        "category_aggregates": [
            {
                "currency": item["currency"],
                "categories": item["spending_by_category"],
            }
            for item in currencies
        ],
        "planning_budget": planning_budget,
        "budget_plan_fingerprint": budget_plan_fingerprint,
    }
    return (
        summary,
        _normalized_issues(blocking),
        _normalized_issues(warnings),
        _normalized_issues(infos),
        fingerprint,
        planning_budget,
        budget_plan_fingerprint,
    )


async def _sync_conflict_counts(
    session: AsyncSession,
    workspace: Workspace,
    closing_start: datetime | None,
    end: datetime,
) -> tuple[int, int]:
    conflicts = list(
        (
            await session.scalars(
                select(SyncConflict).where(
                    SyncConflict.workspace_id == workspace.id,
                    SyncConflict.status == "open",
                )
            )
        ).all()
    )
    transaction_ids = [item.entity_id for item in conflicts if item.entity_type == "transaction"]
    transaction_dates = {
        item.id: item.occurred_at
        for item in (
            list(
                (
                    await session.scalars(
                        select(FinancialTransaction).where(
                            FinancialTransaction.workspace_id == workspace.id,
                            FinancialTransaction.id.in_(transaction_ids),
                        )
                    )
                ).all()
            )
            if transaction_ids
            else []
        )
    }
    in_period = 0
    out_of_period = 0
    for conflict in conflicts:
        if conflict.entity_type != "transaction":
            out_of_period += 1
            continue
        dates: list[datetime] = []
        existing = transaction_dates.get(conflict.entity_id)
        if existing is not None:
            dates.append(existing)
        dates.extend(_payload_dates(conflict.sheet_payload, workspace.timezone))
        # Conservative semantics: an unresolved transaction conflict with an
        # unparseable proposed date may affect the closing interval.
        if not dates or any(
            item < end and (closing_start is None or item >= closing_start) for item in dates
        ):
            in_period += 1
        else:
            out_of_period += 1
    return in_period, out_of_period


def _payload_dates(payload: dict[str, Any], timezone: str) -> list[datetime]:
    values = {
        **dict(payload.get("visible_row", {})),
        **dict(payload.get("changed_fields", {})),
    }
    candidates = [values.get("occurred_at"), values.get("date"), values.get("Дата")]
    result: list[datetime] = []
    for value in candidates:
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.combine(date.fromisoformat(str(value)), time.min)
            except ValueError:
                continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
        result.append(parsed.astimezone(UTC))
    return result


async def _reconciliation_coverage(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    period: date,
    balances: list[Any],
    period_transactions: list[FinancialTransaction],
) -> list[dict[str, object]]:
    balance_by_account = {item.account_id: item for item in balances}
    active_account_ids = (
        {item.account_id for item in period_transactions if item.status in EFFECTIVE_STATUSES}
        | {
            item.target_account_id
            for item in period_transactions
            if item.status in EFFECTIVE_STATUSES and item.target_account_id is not None
        }
        | {account_id for account_id, item in balance_by_account.items() if item.balance != 0}
    )
    if not active_account_ids:
        return []
    accounts = {
        item.id: item
        for item in (
            await session.scalars(
                select(Account).where(
                    Account.workspace_id == workspace_id,
                    Account.id.in_(active_account_ids),
                )
            )
        ).all()
    }
    rows = (
        await session.execute(
            select(
                AccountReconciliation.account_id,
                func.max(AccountReconciliation.statement_date),
            )
            .where(
                AccountReconciliation.workspace_id == workspace_id,
                AccountReconciliation.account_id.in_(active_account_ids),
                AccountReconciliation.status == "confirmed",
            )
            .group_by(AccountReconciliation.account_id)
        )
    ).all()
    latest = {account_id: statement_date for account_id, statement_date in rows}
    required_date = month_end(period)
    result: list[dict[str, object]] = []
    transaction_account_ids = {
        item.account_id for item in period_transactions if item.status in EFFECTIVE_STATUSES
    } | {
        item.target_account_id
        for item in period_transactions
        if item.status in EFFECTIVE_STATUSES and item.target_account_id is not None
    }
    for account_id in sorted(active_account_ids, key=str):
        account = accounts.get(account_id)
        statement_date = latest.get(account_id)
        covered = statement_date is not None and statement_date >= required_date
        balance = balance_by_account.get(account_id)
        reason = (
            "period_activity"
            if account_id in transaction_account_ids
            else "non_zero_period_end_balance"
        )
        result.append(
            {
                "account_id": str(account_id),
                "account_name": account.name if account is not None else "Счёт",
                "account_type": account.account_type if account is not None else "other",
                "currency": (
                    account.currency
                    if account is not None
                    else str(getattr(balance, "currency", ""))
                ),
                "period_end_balance": (str(balance.balance) if balance is not None else None),
                "state": "reconciled" if covered else "not_reconciled",
                "covered": covered,
                "required_statement_date": required_date.isoformat(),
                "latest_statement_date": statement_date.isoformat() if statement_date else None,
                "eligibility_reason": reason,
                "archived": bool(account.is_archived) if account is not None else False,
            }
        )
    return result


def _public_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in snapshot.items() if not key.startswith("_")}


def _snapshot_rows(
    snapshot: dict[str, Any],
    key: str,
    validator: Callable[[dict[str, Any]], bool],
) -> list[dict[str, Any]] | None:
    value = snapshot.get(key)
    if not isinstance(value, list):
        return None
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or not validator(item):
            return None
        result.append(dict(item))
    return result


def _snapshot_optional_int(snapshot: dict[str, Any], key: str) -> int | None:
    value = snapshot.get(key)
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _is_money_string(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        MONEY_ADAPTER.validate_python(value)
    except ValidationError:
        return False
    return True


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_currency_code(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 3
        and value.isascii()
        and value.isalpha()
        and value.isupper()
    )


def _is_uuid_string(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _is_iso_date(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _valid_financial_category(item: dict[str, Any]) -> bool:
    if (
        not isinstance(item.get("name"), str)
        or not _is_money_string(item.get("amount"))
        or not _is_count(item.get("transaction_count"))
    ):
        return False
    try:
        FinancialReportCategory.model_validate(item)
    except ValidationError:
        return False
    return True


def _valid_currency_summary(item: dict[str, Any]) -> bool:
    money_fields = ("income", "expense", "adjustment", "net_cashflow", "transfer_volume")
    category_rows = item.get("spending_by_category")
    monthly_rows = item.get("monthly_comparison")
    expense_rows = item.get("largest_expenses")
    if (
        not _is_currency_code(item.get("currency"))
        or not all(_is_money_string(item.get(key)) for key in money_fields)
        or not _is_count(item.get("transactions_count"))
        or not isinstance(category_rows, list)
        or not all(
            isinstance(row, dict) and _valid_financial_category(row) for row in category_rows
        )
        or not isinstance(monthly_rows, list)
        or not all(
            isinstance(row, dict)
            and all(
                _is_money_string(row.get(key))
                for key in ("income", "expense", "adjustment", "net_cashflow")
            )
            and _is_count(row.get("transactions_count"))
            for row in monthly_rows
        )
        or not isinstance(expense_rows, list)
        or not all(
            isinstance(row, dict) and _is_money_string(row.get("amount")) for row in expense_rows
        )
    ):
        return False
    try:
        FinancialReportGroup.model_validate(item)
    except ValidationError:
        return False
    return True


def _valid_account_balance(item: dict[str, Any]) -> bool:
    if (
        not _is_uuid_string(item.get("account_id"))
        or not isinstance(item.get("name"), str)
        or not _is_currency_code(item.get("currency"))
        or not _is_money_string(item.get("opening_balance"))
        or not _is_money_string(item.get("balance"))
    ):
        return False
    try:
        AccountBalance.model_validate(item)
    except ValidationError:
        return False
    return True


def _valid_category_group(item: dict[str, Any]) -> bool:
    categories = item.get("categories")
    return (
        _is_currency_code(item.get("currency"))
        and isinstance(categories, list)
        and all(
            isinstance(category, dict) and _valid_financial_category(category)
            for category in categories
        )
    )


def _valid_reconciliation_coverage(item: dict[str, Any]) -> bool:
    account_types = {
        "cash",
        "debit_card",
        "credit_card",
        "current_account",
        "savings",
        "deposit",
        "brokerage",
        "crypto_wallet",
        "other",
    }
    state = item.get("state")
    covered = item.get("covered")
    latest_statement_date = item.get("latest_statement_date")
    period_end_balance = item.get("period_end_balance")
    required_keys = {
        "account_id",
        "account_name",
        "account_type",
        "currency",
        "period_end_balance",
        "state",
        "covered",
        "required_statement_date",
        "latest_statement_date",
        "eligibility_reason",
        "archived",
    }
    return (
        required_keys.issubset(item)
        and _is_uuid_string(item.get("account_id"))
        and isinstance(item.get("account_name"), str)
        and item.get("account_type") in account_types
        and _is_currency_code(item.get("currency"))
        and (period_end_balance is None or _is_money_string(period_end_balance))
        and state in {"reconciled", "not_reconciled"}
        and isinstance(covered, bool)
        and covered == (state == "reconciled")
        and _is_iso_date(item.get("required_statement_date"))
        and (latest_statement_date is None or _is_iso_date(latest_statement_date))
        and item.get("eligibility_reason") in {"period_activity", "non_zero_period_end_balance"}
        and isinstance(item.get("archived"), bool)
    )


def _valid_issue(item: dict[str, Any], severity: str) -> bool:
    return (
        isinstance(item.get("code"), str)
        and item.get("severity") == severity
        and isinstance(item.get("scope"), str)
        and _is_count(item.get("count"))
        and isinstance(item.get("message"), str)
        and isinstance(item.get("details"), dict)
    )


def _snapshot_issues(snapshot: dict[str, Any]) -> dict[str, list[dict[str, Any]]] | None:
    value = snapshot.get("issues")
    if not isinstance(value, dict):
        return None
    result: dict[str, list[dict[str, Any]]] = {}
    for bucket, severity in (
        ("blocking", "blocker"),
        ("warnings", "warning"),
        ("info", "info"),
    ):
        rows = value.get(bucket)
        if not isinstance(rows, list):
            return None
        parsed: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict) or not _valid_issue(row, severity):
                return None
            parsed.append(dict(row))
        result[bucket] = parsed
    return result


def _as_dict_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _as_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _actor(user: User | None, user_id: uuid.UUID) -> dict[str, object]:
    return {
        "id": user_id,
        "display_name": user.display_name if user is not None else "Finspace user",
        "display_name_source": "current_profile",
    }


async def _revision_rows(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    period: date,
    *,
    order: str,
    limit: int,
    offset: int,
) -> tuple[MonthClosure, list[MonthCloseRevision], int]:
    closure = await get_closure(session, workspace_id, period)
    filters = (
        MonthCloseRevision.workspace_id == workspace_id,
        MonthCloseRevision.closure_id == closure.id,
        MonthCloseRevision.period_month == period,
    )
    total = int(
        await session.scalar(select(func.count()).select_from(MonthCloseRevision).where(*filters))
        or 0
    )
    ordering = (
        (MonthCloseRevision.revision_number.asc(), MonthCloseRevision.id.asc())
        if order == "oldest"
        else (MonthCloseRevision.revision_number.desc(), MonthCloseRevision.id.desc())
    )
    rows = list(
        (
            await session.scalars(
                select(MonthCloseRevision)
                .where(*filters)
                .order_by(*ordering)
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return closure, rows, total


async def _reopen_metadata(
    session: AsyncSession,
    closure: MonthClosure,
    revisions: list[MonthCloseRevision],
) -> dict[int, dict[str, object]]:
    if not revisions:
        return {}
    all_revisions = list(
        (
            await session.scalars(
                select(MonthCloseRevision)
                .where(
                    MonthCloseRevision.workspace_id == closure.workspace_id,
                    MonthCloseRevision.closure_id == closure.id,
                )
                .order_by(MonthCloseRevision.revision_number.asc())
            )
        ).all()
    )
    audits = list(
        (
            await session.scalars(
                select(AuditLog)
                .where(
                    AuditLog.workspace_id == closure.workspace_id,
                    AuditLog.entity_id == closure.id,
                    AuditLog.action == "month_close.reopen",
                )
                .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
            )
        ).all()
    )
    actor_ids = {item.actor_user_id for item in audits if item.actor_user_id is not None}
    users = {
        item.id: item
        for item in (
            list((await session.scalars(select(User).where(User.id.in_(actor_ids)))).all())
            if actor_ids
            else []
        )
    }
    result: dict[int, dict[str, object]] = {}
    for index, revision in enumerate(all_revisions):
        next_confirmed_at = (
            all_revisions[index + 1].confirmed_at if index + 1 < len(all_revisions) else None
        )
        audit = next(
            (
                item
                for item in audits
                if item.created_at >= revision.confirmed_at
                and (next_confirmed_at is None or item.created_at < next_confirmed_at)
            ),
            None,
        )
        if audit is None:
            continue
        after = dict(audit.after_data or {})
        result[revision.revision_number] = {
            "reopened_at": audit.created_at,
            "reopened_by": (
                _actor(users.get(audit.actor_user_id), audit.actor_user_id)
                if audit.actor_user_id is not None
                else None
            ),
            "reason": str(after["reason"]) if after.get("reason") is not None else None,
        }
    return result


async def list_history(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    period: date,
    *,
    order: str,
    limit: int,
    offset: int,
) -> tuple[MonthClosure, list[dict[str, object]], int]:
    closure, revisions, total = await _revision_rows(
        session,
        workspace_id,
        period,
        order=order,
        limit=limit,
        offset=offset,
    )
    user_ids = {item.confirmed_by for item in revisions}
    users = {
        item.id: item
        for item in (
            list((await session.scalars(select(User).where(User.id.in_(user_ids)))).all())
            if user_ids
            else []
        )
    }
    reopen = await _reopen_metadata(session, closure, revisions)
    return (
        closure,
        [
            {
                "id": item.id,
                "revision_number": item.revision_number,
                "period_month": item.period_month,
                "period_start_at": item.period_start_at,
                "period_end_at": item.period_end_at,
                "confirmed_at": item.confirmed_at,
                "confirmed_by": _actor(users.get(item.confirmed_by), item.confirmed_by),
                "financial_fingerprint": item.financial_fingerprint,
                "legacy_unverified": item.legacy_unverified,
                "source": item.source,
                "snapshot_summary": _public_snapshot(dict(item.snapshot)),
                "reopened": reopen.get(item.revision_number),
            }
            for item in revisions
        ],
        total,
    )


async def get_revision(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    period: date,
    revision_number: int,
) -> MonthCloseRevision:
    revision = await session.scalar(
        select(MonthCloseRevision)
        .join(MonthClosure, MonthClosure.id == MonthCloseRevision.closure_id)
        .where(
            MonthCloseRevision.workspace_id == workspace_id,
            MonthCloseRevision.period_month == period,
            MonthCloseRevision.revision_number == revision_number,
            MonthClosure.workspace_id == workspace_id,
            MonthClosure.period_month == period,
        )
    )
    if revision is None:
        raise ApiError(
            status_code=404,
            code="MONTH_CLOSE_REVISION_NOT_FOUND",
            message="Month close revision was not found",
        )
    return revision


async def revision_detail(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    period: date,
    revision_number: int,
) -> dict[str, object]:
    revision = await get_revision(session, workspace_id, period, revision_number)
    user = await session.get(User, revision.confirmed_by)
    closure = await get_closure(session, workspace_id, period)
    reopen = await _reopen_metadata(session, closure, [revision])
    return {
        "id": revision.id,
        "revision_number": revision.revision_number,
        "period_month": revision.period_month,
        "period_start_at": revision.period_start_at,
        "period_end_at": revision.period_end_at,
        "confirmed_at": revision.confirmed_at,
        "confirmed_by": _actor(user, revision.confirmed_by),
        "financial_fingerprint": revision.financial_fingerprint,
        "legacy_unverified": revision.legacy_unverified,
        "source": revision.source,
        "snapshot_summary": _public_snapshot(dict(revision.snapshot)),
        "reopened": reopen.get(revision.revision_number),
    }


async def as_closed_report(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    period: date,
    revision_number: int,
) -> dict[str, object]:
    revision = await get_revision(session, workspace_id, period, revision_number)
    user = await session.get(User, revision.confirmed_by)
    snapshot = _public_snapshot(dict(revision.snapshot))
    issues = _snapshot_issues(snapshot)
    period_value = snapshot.get("period")
    sections: dict[str, object | None] = {
        "currencies": _snapshot_rows(snapshot, "currencies", _valid_currency_summary),
        "account_balances": _snapshot_rows(snapshot, "account_balances", _valid_account_balance),
        "category_aggregates": _snapshot_rows(
            snapshot, "category_aggregates", _valid_category_group
        ),
        "transaction_count": _snapshot_optional_int(snapshot, "transaction_count"),
        "reconciliation_coverage": _snapshot_rows(
            snapshot, "reconciliation_coverage", _valid_reconciliation_coverage
        ),
        "issue_summary": (
            {
                "blocker_count": len(issues["blocking"]),
                "warning_count": len(issues["warnings"]),
                "info_count": len(issues["info"]),
                "blockers": issues["blocking"],
                "warnings": issues["warnings"],
                "info": issues["info"],
            }
            if issues is not None
            else None
        ),
    }
    return {
        "mode": "as_closed",
        "period": (
            dict(period_value)
            if isinstance(period_value, dict)
            else {
                "month": period.isoformat(),
                "start_at": revision.period_start_at.isoformat(),
                "end_at": revision.period_end_at.isoformat(),
            }
        ),
        "revision_number": revision.revision_number,
        "confirmed_at": revision.confirmed_at,
        "confirmed_by": _actor(user, revision.confirmed_by),
        "legacy_unverified": revision.legacy_unverified,
        "financial_fingerprint": revision.financial_fingerprint,
        **sections,
        "unavailable_sections": [key for key, value in sections.items() if value is None],
    }


def _values_by_key(items: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {str(item.get(key)): item for item in items}


def _comparison_items(
    closed_items: list[dict[str, Any]],
    current_items: list[dict[str, Any]],
    *,
    key: str,
) -> list[dict[str, object]]:
    closed = _values_by_key(closed_items, key)
    current = _values_by_key(current_items, key)
    return [
        {
            key: item_key,
            "as_closed": closed.get(item_key),
            "current": current.get(item_key),
            "changed": closed.get(item_key) != current.get(item_key),
        }
        for item_key in sorted(set(closed) | set(current))
    ]


async def compare_with_current(
    session: AsyncSession,
    workspace: Workspace,
    period: date,
    revision_number: int,
) -> dict[str, object]:
    closed = await as_closed_report(session, workspace.id, period, revision_number)
    unavailable = set(_as_string_list(closed.get("unavailable_sections")))
    _, end = period_bounds(period, workspace.timezone)
    report = await financial_report(
        session,
        workspace,
        date_from=period,
        date_to=month_end(period),
        currency=None,
    )
    currencies = [item.model_dump(mode="json") for item in report.groups]
    comparable_currencies = [
        {
            key: item[key]
            for key in (
                "currency",
                "income",
                "expense",
                "adjustment",
                "net_cashflow",
                "transfer_volume",
                "transactions_count",
            )
        }
        for item in currencies
    ]
    balances = await calculate_balances(session, workspace.id, as_of=end)
    current_balances = [item.model_dump(mode="json") for item in balances]
    current_categories = [
        {"currency": item["currency"], "categories": item["spending_by_category"]}
        for item in currencies
    ]
    period_transactions = list(
        (
            await session.scalars(
                select(FinancialTransaction).where(
                    FinancialTransaction.workspace_id == workspace.id,
                    FinancialTransaction.occurred_at >= report.period.cutoff_from,
                    FinancialTransaction.occurred_at < report.period.cutoff_to,
                    FinancialTransaction.deleted_at.is_(None),
                )
            )
        ).all()
    )
    coverage = await _reconciliation_coverage(
        session, workspace.id, period, balances, period_transactions
    )
    current = {
        "mode": "current",
        "period": report.period.model_dump(mode="json"),
        "currencies": currencies,
        "account_balances": current_balances,
        "category_aggregates": current_categories,
        "transaction_count": sum(int(item["transactions_count"]) for item in currencies),
        "reconciliation_coverage": coverage,
    }
    closed_currencies = [
        {
            key: item.get(key)
            for key in (
                "currency",
                "income",
                "expense",
                "adjustment",
                "net_cashflow",
                "transfer_volume",
                "transactions_count",
            )
        }
        for item in _as_dict_list(closed.get("currencies"))
    ]
    closed_balances = _as_dict_list(closed.get("account_balances"))
    closed_categories = _as_dict_list(closed.get("category_aggregates"))
    return {
        "period_month": period,
        "revision_number": revision_number,
        "as_closed": closed,
        "current": current,
        "differences": {
            "currencies": (
                []
                if "currencies" in unavailable
                else _comparison_items(closed_currencies, comparable_currencies, key="currency")
            ),
            "account_balances": (
                []
                if "account_balances" in unavailable
                else _comparison_items(
                    closed_balances,
                    current_balances,
                    key="account_id",
                )
            ),
            "category_aggregates": (
                []
                if "category_aggregates" in unavailable
                else _comparison_items(
                    closed_categories,
                    current_categories,
                    key="currency",
                )
            ),
        },
        "unavailable_sections": sorted(unavailable),
    }


async def _count(session: AsyncSession, query: Any) -> int:
    return len(list((await session.scalars(query)).all()))
