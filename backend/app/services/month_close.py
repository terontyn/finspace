import uuid
from collections import Counter
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models.account_reconciliation import AccountReconciliation
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
from app.db.models.users import Workspace
from app.dependencies.context import RequestContext
from app.services.audit import record_audit, request_uuid
from app.services.backup_status import get_backup_status
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
            details={"period": period.strftime("%Y-%m")},
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


def _expected_next_period(control: MonthCloseControl) -> date | None:
    if control.closed_through is None:
        return None
    return next_month(date(control.closed_through.year, control.closed_through.month, 1))


def _sequence_issue(control: MonthCloseControl, period: date) -> dict[str, object] | None:
    expected = _expected_next_period(control)
    if expected is None or period == expected:
        return None
    return {
        "code": "MONTH_CLOSE_SEQUENCE_CONFLICT",
        "expected_period": expected.strftime("%Y-%m"),
        "requested_period": period.strftime("%Y-%m"),
    }


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
    return {
        "code": "MONTH_CLOSE_SEQUENCE_CONFLICT",
        "requested_period": period.strftime("%Y-%m"),
        "reason": "legacy_confirmed_history_is_ambiguous",
    }


def _normalized_issues(items: list[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(items, key=hash_canonical)


def _prepare_token(
    closure: MonthClosure,
    *,
    generation: int,
    financial_hash: str,
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
    closure = await _get_or_create_closure(session, workspace.id, period)
    if closure.status == "confirmed":
        raise ApiError(
            status_code=409,
            code="MONTH_ALREADY_CLOSED",
            message="Month is already closed",
        )
    control = await get_or_create_control(session, workspace.id, for_update=False)
    summary, blocking, warnings, infos, fingerprint = await collect_preview(
        session, workspace, period, control
    )
    generation = closure.version + 1
    token = _prepare_token(
        closure,
        generation=generation,
        financial_hash=fingerprint,
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
    summary, blocking, warnings, infos, fingerprint = await collect_preview(
        session, context.workspace, period, control
    )
    actual_token = _prepare_token(
        closure,
        generation=closure.version,
        financial_hash=fingerprint,
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
        blocking.append({"code": "DRAFT_TRANSACTIONS", "count": draft_count})

    uncategorized = sum(
        item.status != "cancelled"
        and item.transaction_type in {"income", "expense"}
        and item.category_id is None
        for item in period_transactions
    )
    if uncategorized:
        warnings.append({"code": "UNCATEGORIZED_TRANSACTIONS", "count": uncategorized})

    in_period_conflicts, out_of_period_conflicts = await _sync_conflict_counts(
        session, workspace, closing_start, end
    )
    if in_period_conflicts:
        blocking.append({"code": "SYNC_CONFLICTS_IN_PERIOD", "count": in_period_conflicts})
    if out_of_period_conflicts:
        infos.append({"code": "OUT_OF_PERIOD_SYNC_CONFLICTS", "count": out_of_period_conflicts})

    failed_outbox = await _count(
        session,
        select(SyncOutbox.id).where(
            SyncOutbox.workspace_id == workspace.id,
            SyncOutbox.status == "failed",
        ),
    )
    if failed_outbox:
        warnings.append({"code": "FAILED_SYNC_OUTBOX", "count": failed_outbox})

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
        warnings.append(
            {
                "code": "IMPORT_ROWS_REQUIRING_ATTENTION",
                "count": invalid_import_rows,
            }
        )
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
        infos.append({"code": "STAGED_IMPORTS", "count": staged_imports})

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
        warnings.append({"code": "FAILED_RECURRING_EXECUTIONS", "count": failed_recurring})

    backup = await get_backup_status(session)
    if backup.status != "healthy":
        backup_issue: dict[str, object] = {
            "code": f"BACKUP_{backup.status.upper()}",
            "status": backup.status,
        }
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
        warnings.append({"code": "POSSIBLE_DUPLICATES", "count": duplicate_count})

    balances = await calculate_balances(session, workspace.id, as_of=end)
    negative = [item for item in balances if item.balance < Decimal("0")]
    if negative:
        warnings.append({"code": "NEGATIVE_PERIOD_END_BALANCES", "count": len(negative)})
    reconciliation_coverage = await _reconciliation_coverage(
        session, workspace.id, period, balances, period_transactions
    )
    unreconciled = [item for item in reconciliation_coverage if not item["covered"]]
    if unreconciled:
        warnings.append({"code": "ACCOUNT_NOT_RECONCILED", "count": len(unreconciled)})

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
        infos.append({"code": "NO_FINANCIAL_ACTIVITY", "count": 0})
    fingerprint = await financial_fingerprint(session, workspace.id, end)
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
    }
    return (
        summary,
        _normalized_issues(blocking),
        _normalized_issues(warnings),
        _normalized_issues(infos),
        fingerprint,
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
    rows = (
        await session.execute(
            select(
                AccountReconciliation.account_id,
                func.max(AccountReconciliation.statement_date),
            )
            .where(
                AccountReconciliation.workspace_id == workspace_id,
                AccountReconciliation.account_id.in_(active_account_ids),
            )
            .group_by(AccountReconciliation.account_id)
        )
    ).all()
    latest = {account_id: statement_date for account_id, statement_date in rows}
    required_date = month_end(period)
    return [
        {
            "account_id": str(account_id),
            "covered": latest.get(account_id) is not None and latest[account_id] >= required_date,
            "latest_statement_date": (
                latest[account_id].isoformat() if latest.get(account_id) else None
            ),
        }
        for account_id in sorted(active_account_ids, key=str)
    ]


async def _count(session: AsyncSession, query: Any) -> int:
    return len(list((await session.scalars(query)).all()))
