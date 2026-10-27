import hashlib
import re
import unicodedata
import uuid
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models.payees import Payee, PayeeAlias
from app.dependencies.context import RequestContext
from app.repositories import payees as repository
from app.schemas.payees import (
    PayeeAliasCreate,
    PayeeAliasResponse,
    PayeeCreate,
    PayeeResponse,
    PayeeUpdate,
)
from app.services.audit import record_audit, snapshot

_UNICODE_WHITESPACE = re.compile(r"\s+", flags=re.UNICODE)


def normalize_alias(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = _UNICODE_WHITESPACE.sub(" ", normalized.strip())
    return unicodedata.normalize("NFKC", normalized.casefold())


def normalized_alias_hash(normalized_alias: str) -> str:
    return hashlib.sha256(normalized_alias.encode("utf-8")).hexdigest()


def alias_identity(value: str) -> tuple[str, str]:
    normalized = normalize_alias(value)
    if not normalized:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="Payee alias must contain visible characters",
        )
    return normalized, normalized_alias_hash(normalized)


def _check_version(payee: Payee, version: int) -> None:
    if payee.version != version:
        raise ApiError(status_code=409, code="VERSION_CONFLICT", message="Version is stale")


def _not_found() -> ApiError:
    return ApiError(status_code=404, code="PAYEE_NOT_FOUND", message="Payee was not found")


def _alias_not_found() -> ApiError:
    return ApiError(
        status_code=404,
        code="PAYEE_ALIAS_NOT_FOUND",
        message="Payee alias was not found",
    )


def _raise_alias_conflict(candidate: PayeeAlias, normalized: str) -> None:
    if candidate.normalized_alias != normalized:
        raise ApiError(
            status_code=409,
            code="PAYEE_ALIAS_HASH_COLLISION",
            message="Payee alias hash collision was detected",
        )
    raise ApiError(
        status_code=409,
        code="PAYEE_ALIAS_CONFLICT",
        message="Payee alias is already reserved",
        details={"payee_id": str(candidate.payee_id)},
    )


async def _raise_integrity_alias_error(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    normalized: str,
    alias_hash: str,
) -> None:
    candidate = await repository.find_alias_candidate(session, workspace_id, alias_hash)
    if candidate is not None:
        _raise_alias_conflict(candidate, normalized)
    raise ApiError(
        status_code=409,
        code="PAYEE_ALIAS_CONFLICT",
        message="Payee alias could not be reserved",
    )


def _primary_alias(payee: Payee) -> PayeeAlias:
    primary = [alias for alias in payee.aliases if alias.deleted_at is None and alias.is_primary]
    if len(primary) != 1:
        raise ApiError(
            status_code=409,
            code="PAYEE_PRIMARY_ALIAS_REQUIRED",
            message="Payee must have exactly one current primary alias",
        )
    return primary[0]


async def _loaded_payee(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    payee_id: uuid.UUID,
) -> Payee:
    payee = await repository.get_payee(
        session,
        workspace_id,
        payee_id,
        include_deleted=True,
        include_aliases=True,
    )
    if payee is None:
        raise _not_found()
    return payee


def payee_response(payee: Payee) -> PayeeResponse:
    aliases = sorted(
        payee.aliases,
        key=lambda item: (
            not item.is_primary,
            item.deleted_at is not None,
            item.alias.casefold(),
            item.id,
        ),
    )
    return PayeeResponse(
        id=payee.id,
        name=payee.name,
        notes=payee.notes,
        aliases=[PayeeAliasResponse.model_validate(alias) for alias in aliases],
        version=payee.version,
        created_at=payee.created_at,
        updated_at=payee.updated_at,
        deleted_at=payee.deleted_at,
    )


async def get_assignable_payee_for_write(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    payee_id: uuid.UUID,
) -> Payee:
    payee = await repository.get_payee(
        session,
        workspace_id,
        payee_id,
        for_share=True,
    )
    if payee is None:
        raise _not_found()
    return payee


async def resolve_exact_alias(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    value: str,
) -> Payee | None:
    normalized, alias_hash = alias_identity(value)
    candidate = await repository.find_alias_candidate(session, workspace_id, alias_hash)
    if candidate is None:
        return None
    if candidate.normalized_alias != normalized:
        _raise_alias_conflict(candidate, normalized)
    if candidate.deleted_at is not None:
        return None
    return await repository.get_payee(session, workspace_id, candidate.payee_id)


async def create_payee(
    session: AsyncSession,
    context: RequestContext,
    data: PayeeCreate,
) -> Payee:
    normalized, alias_hash = alias_identity(data.name)
    candidate = await repository.find_alias_candidate(session, context.workspace.id, alias_hash)
    if candidate is not None:
        _raise_alias_conflict(candidate, normalized)

    payee_id = uuid.uuid4()
    payee = Payee(
        id=payee_id,
        workspace_id=context.workspace.id,
        name=data.name,
        notes=data.notes,
        created_by=context.user.id,
        updated_by=context.user.id,
    )
    primary = PayeeAlias(
        workspace_id=context.workspace.id,
        payee_id=payee_id,
        alias=data.name,
        normalized_alias=normalized,
        normalized_alias_hash=alias_hash,
        is_primary=True,
        created_by=context.user.id,
    )
    try:
        async with session.begin_nested():
            session.add_all([payee, primary])
            await session.flush()
    except IntegrityError:
        await _raise_integrity_alias_error(session, context.workspace.id, normalized, alias_hash)
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="payee",
        entity_id=payee.id,
        action="create",
        before_data=None,
        after_data=snapshot("payee", payee),
        request_id=context.request_id,
    )
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="payee_alias",
        entity_id=primary.id,
        action="create",
        before_data=None,
        after_data=snapshot("payee_alias", primary),
        request_id=context.request_id,
    )
    await session.commit()
    return await _loaded_payee(session, context.workspace.id, payee.id)


async def update_payee(
    session: AsyncSession,
    context: RequestContext,
    payee_id: uuid.UUID,
    data: PayeeUpdate,
) -> Payee:
    payee = await repository.get_payee(
        session,
        context.workspace.id,
        payee_id,
        for_update=True,
        include_aliases=True,
    )
    if payee is None:
        raise _not_found()
    _check_version(payee, data.version)
    before_payee = snapshot("payee", payee)
    changes = data.model_dump(exclude_unset=True, exclude={"version"})
    now = datetime.now(UTC)
    normalized = ""
    alias_hash = ""

    try:
        async with session.begin_nested():
            if "name" in changes:
                name = str(changes["name"])
                normalized, alias_hash = alias_identity(name)
                old_primary = _primary_alias(payee)
                if old_primary.normalized_alias == normalized:
                    if old_primary.alias != name:
                        old_primary.alias = name
                        old_primary.updated_at = now
                else:
                    candidate = await repository.find_alias_candidate(
                        session, context.workspace.id, alias_hash
                    )
                    if candidate is not None:
                        if candidate.normalized_alias != normalized:
                            _raise_alias_conflict(candidate, normalized)
                        if candidate.payee_id != payee.id:
                            _raise_alias_conflict(candidate, normalized)

                    old_primary.is_primary = False
                    old_primary.updated_at = now
                    # Partial unique indexes are immediate. Persist the demotion first so
                    # promotion/reuse cannot transiently expose two primary rows.
                    await session.flush()

                    if candidate is None:
                        candidate = PayeeAlias(
                            workspace_id=context.workspace.id,
                            payee_id=payee.id,
                            alias=name,
                            normalized_alias=normalized,
                            normalized_alias_hash=alias_hash,
                            is_primary=True,
                            created_by=context.user.id,
                        )
                        session.add(candidate)
                    else:
                        candidate.alias = name
                        candidate.deleted_at = None
                        candidate.is_primary = True
                        candidate.updated_at = now
                payee.name = name
            if "notes" in changes:
                payee.notes = data.notes
            payee.updated_by = context.user.id
            payee.updated_at = now
            payee.version += 1
            await session.flush()
    except IntegrityError:
        await _raise_integrity_alias_error(session, context.workspace.id, normalized, alias_hash)

    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="payee",
        entity_id=payee.id,
        action="update",
        before_data=before_payee,
        after_data=snapshot("payee", payee),
        request_id=context.request_id,
    )
    await session.commit()
    return await _loaded_payee(session, context.workspace.id, payee.id)


async def create_alias(
    session: AsyncSession,
    context: RequestContext,
    payee_id: uuid.UUID,
    data: PayeeAliasCreate,
) -> Payee:
    payee = await repository.get_payee(
        session,
        context.workspace.id,
        payee_id,
        for_update=True,
        include_aliases=True,
    )
    if payee is None:
        raise _not_found()
    _check_version(payee, data.version)
    normalized, alias_hash = alias_identity(data.alias)
    candidate = await repository.find_alias_candidate(session, context.workspace.id, alias_hash)
    if candidate is not None:
        _raise_alias_conflict(candidate, normalized)
    before_payee = snapshot("payee", payee)
    alias = PayeeAlias(
        workspace_id=context.workspace.id,
        payee_id=payee.id,
        alias=data.alias,
        normalized_alias=normalized,
        normalized_alias_hash=alias_hash,
        is_primary=False,
        created_by=context.user.id,
    )
    now = datetime.now(UTC)
    try:
        async with session.begin_nested():
            session.add(alias)
            payee.updated_by = context.user.id
            payee.updated_at = now
            payee.version += 1
            await session.flush()
    except IntegrityError:
        await _raise_integrity_alias_error(session, context.workspace.id, normalized, alias_hash)
    await _audit_alias_and_parent(session, context, payee, alias, "create", None, before_payee)
    await session.commit()
    return await _loaded_payee(session, context.workspace.id, payee.id)


async def delete_alias(
    session: AsyncSession,
    context: RequestContext,
    payee_id: uuid.UUID,
    alias_id: uuid.UUID,
    version: int,
) -> Payee:
    payee = await repository.get_payee(
        session,
        context.workspace.id,
        payee_id,
        for_update=True,
        include_aliases=True,
    )
    if payee is None:
        raise _not_found()
    _check_version(payee, version)
    alias = await repository.get_alias(session, context.workspace.id, payee.id, alias_id)
    if alias is None:
        raise _alias_not_found()
    if alias.is_primary:
        raise ApiError(
            status_code=409,
            code="PAYEE_PRIMARY_ALIAS_REQUIRED",
            message="Primary Payee alias cannot be deleted",
        )
    before_payee = snapshot("payee", payee)
    before_alias = snapshot("payee_alias", alias)
    now = datetime.now(UTC)
    alias.deleted_at = now
    alias.updated_at = now
    payee.updated_by = context.user.id
    payee.updated_at = now
    payee.version += 1
    await session.flush()
    await _audit_alias_and_parent(
        session, context, payee, alias, "delete", before_alias, before_payee
    )
    await session.commit()
    return await _loaded_payee(session, context.workspace.id, payee.id)


async def restore_alias(
    session: AsyncSession,
    context: RequestContext,
    payee_id: uuid.UUID,
    alias_id: uuid.UUID,
    version: int,
) -> Payee:
    payee = await repository.get_payee(
        session,
        context.workspace.id,
        payee_id,
        for_update=True,
        include_aliases=True,
    )
    if payee is None:
        raise _not_found()
    _check_version(payee, version)
    alias = await repository.get_alias(
        session,
        context.workspace.id,
        payee.id,
        alias_id,
        include_deleted=True,
    )
    if alias is None:
        raise _alias_not_found()
    if alias.deleted_at is None:
        return payee
    candidate = await repository.find_alias_candidate(
        session, context.workspace.id, alias.normalized_alias_hash
    )
    if candidate is None or candidate.id != alias.id:
        if candidate is not None:
            _raise_alias_conflict(candidate, alias.normalized_alias)
        raise ApiError(
            status_code=409,
            code="PAYEE_ALIAS_CONFLICT",
            message="Payee alias reservation is missing",
        )
    if candidate.normalized_alias != alias.normalized_alias:
        _raise_alias_conflict(candidate, alias.normalized_alias)
    before_payee = snapshot("payee", payee)
    before_alias = snapshot("payee_alias", alias)
    now = datetime.now(UTC)
    alias.deleted_at = None
    alias.updated_at = now
    payee.updated_by = context.user.id
    payee.updated_at = now
    payee.version += 1
    await session.flush()
    await _audit_alias_and_parent(
        session, context, payee, alias, "restore", before_alias, before_payee
    )
    await session.commit()
    return await _loaded_payee(session, context.workspace.id, payee.id)


async def delete_payee(
    session: AsyncSession,
    context: RequestContext,
    payee_id: uuid.UUID,
    version: int,
) -> Payee:
    payee = await repository.get_payee(
        session,
        context.workspace.id,
        payee_id,
        for_update=True,
        include_aliases=True,
    )
    if payee is None:
        raise _not_found()
    _check_version(payee, version)
    before = snapshot("payee", payee)
    now = datetime.now(UTC)
    payee.deleted_at = now
    payee.updated_at = now
    payee.updated_by = context.user.id
    payee.version += 1
    await session.flush()
    await _audit_payee(session, context, payee, "delete", before)
    await session.commit()
    return await _loaded_payee(session, context.workspace.id, payee.id)


async def restore_payee(
    session: AsyncSession,
    context: RequestContext,
    payee_id: uuid.UUID,
    version: int,
) -> Payee:
    payee = await repository.get_payee(
        session,
        context.workspace.id,
        payee_id,
        include_deleted=True,
        for_update=True,
        include_aliases=True,
    )
    if payee is None:
        raise _not_found()
    _check_version(payee, version)
    if payee.deleted_at is None:
        return payee
    _primary_alias(payee)
    for alias in payee.aliases:
        candidate = await repository.find_alias_candidate(
            session, context.workspace.id, alias.normalized_alias_hash
        )
        if candidate is None or candidate.id != alias.id:
            if candidate is not None:
                _raise_alias_conflict(candidate, alias.normalized_alias)
            raise ApiError(
                status_code=409,
                code="PAYEE_ALIAS_CONFLICT",
                message="Payee alias reservation is missing",
            )
        if candidate.normalized_alias != alias.normalized_alias:
            _raise_alias_conflict(candidate, alias.normalized_alias)
    before = snapshot("payee", payee)
    payee.deleted_at = None
    payee.updated_at = datetime.now(UTC)
    payee.updated_by = context.user.id
    payee.version += 1
    await session.flush()
    await _audit_payee(session, context, payee, "restore", before)
    await session.commit()
    return await _loaded_payee(session, context.workspace.id, payee.id)


async def _audit_payee(
    session: AsyncSession,
    context: RequestContext,
    payee: Payee,
    action: str,
    before: dict[str, object] | None,
) -> None:
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="payee",
        entity_id=payee.id,
        action=action,
        before_data=before,
        after_data=snapshot("payee", payee),
        request_id=context.request_id,
    )


async def _audit_alias_and_parent(
    session: AsyncSession,
    context: RequestContext,
    payee: Payee,
    alias: PayeeAlias,
    action: str,
    before_alias: dict[str, object] | None,
    before_payee: dict[str, object],
) -> None:
    await record_audit(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        entity_type="payee_alias",
        entity_id=alias.id,
        action=action,
        before_data=before_alias,
        after_data=snapshot("payee_alias", alias),
        request_id=context.request_id,
    )
    await _audit_payee(session, context, payee, "update", before_payee)
