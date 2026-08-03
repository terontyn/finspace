import uuid
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ApiError
from app.core.security import (
    constant_hash_match,
    create_access_token,
    hash_client_value,
    hash_password,
    hash_refresh_secret,
    new_refresh_secret,
    normalize_email,
    verify_password,
)
from app.db.models.auth import AuthSession
from app.db.models.users import User, Workspace, WorkspaceMember
from app.schemas.auth import AuthResponse, RegisterRequest
from app.schemas.users import UserResponse, WorkspaceResponse
from app.services.audit import record_audit

MAX_LOGIN_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60
LOCK_MINUTES = 15


def _auth_response(user: User, workspace: Workspace) -> AuthResponse:
    return AuthResponse(
        access_token=create_access_token(user.id),
        expires_in=settings.access_token_ttl_minutes * 60,
        user=UserResponse.model_validate(user),
        workspace=WorkspaceResponse.model_validate(workspace),
    )


async def _workspace_for_user(session: AsyncSession, user_id: uuid.UUID) -> Workspace:
    workspace = await session.scalar(
        select(Workspace)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(
            WorkspaceMember.user_id == user_id,
            Workspace.deleted_at.is_(None),
        )
        .order_by(Workspace.created_at, Workspace.id)
    )
    if workspace is None:
        raise ApiError(status_code=403, code="WORKSPACE_ACCESS_DENIED", message="No workspace")
    return workspace


async def _rate_key_count(email: str, ip: str | None) -> int:
    client = Redis.from_url(settings.redis_url_value, decode_responses=True)
    email_key = f"auth:login:email:{hash_client_value(email)}"
    ip_key = f"auth:login:ip:{hash_client_value(ip) or 'unknown'}"
    try:
        email_count = int(await client.get(email_key) or 0)
        ip_count = int(await client.get(ip_key) or 0)
        return max(email_count, ip_count)
    finally:
        await client.aclose()


async def _record_failure(email: str, ip: str | None) -> int:
    client = Redis.from_url(settings.redis_url_value, decode_responses=True)
    keys = (
        f"auth:login:email:{hash_client_value(email)}",
        f"auth:login:ip:{hash_client_value(ip) or 'unknown'}",
    )
    try:
        counts: list[int] = []
        for key in keys:
            count = int(await client.incr(key))
            if count == 1:
                await client.expire(key, LOGIN_WINDOW_SECONDS)
            counts.append(count)
        return max(counts)
    finally:
        await client.aclose()


async def _reset_rate_limit(email: str, ip: str | None) -> None:
    client = Redis.from_url(settings.redis_url_value, decode_responses=True)
    try:
        await client.delete(
            f"auth:login:email:{hash_client_value(email)}",
            f"auth:login:ip:{hash_client_value(ip) or 'unknown'}",
        )
    finally:
        await client.aclose()


async def create_refresh_session(
    session: AsyncSession,
    user: User,
    *,
    user_agent: str | None,
    ip: str | None,
) -> tuple[AuthSession, str]:
    now = datetime.now(UTC)
    secret = new_refresh_secret()
    auth_session = AuthSession(
        user_id=user.id,
        refresh_token_hash=hash_refresh_secret(secret),
        user_agent_hash=hash_client_value(user_agent),
        ip_hash=hash_client_value(ip),
        created_at=now,
        last_used_at=now,
        expires_at=now + timedelta(days=settings.refresh_token_ttl_days),
    )
    session.add(auth_session)
    await session.flush()
    return auth_session, f"{auth_session.id}.{secret}"


async def register_user(
    session: AsyncSession,
    data: RegisterRequest,
    *,
    request_id: str,
    user_agent: str | None,
    ip: str | None,
) -> tuple[AuthResponse, str]:
    if settings.environment != "development" or not settings.allow_registration:
        raise ApiError(status_code=404, code="HTTP_ERROR", message="Registration is disabled")
    normalized_email = normalize_email(data.email)
    existing = await session.scalar(
        select(User.id).where(User.normalized_email == normalized_email)
    )
    if existing is not None:
        raise ApiError(status_code=409, code="DUPLICATE_EMAIL", message="Email is already used")
    user = User(
        email=data.email.strip(),
        normalized_email=normalized_email,
        display_name=data.display_name,
        timezone=data.timezone,
        password_hash=hash_password(data.password),
    )
    session.add(user)
    await session.flush()
    workspace = Workspace(
        name=data.workspace_name,
        base_currency=data.base_currency,
        timezone=data.timezone,
        owner_user_id=user.id,
    )
    session.add(workspace)
    await session.flush()
    session.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="owner"))
    _, refresh_token = await create_refresh_session(session, user, user_agent=user_agent, ip=ip)
    await record_audit(
        session,
        workspace_id=workspace.id,
        actor_user_id=user.id,
        entity_type="user",
        entity_id=user.id,
        action="user.register",
        before_data=None,
        after_data={"user_id": str(user.id), "workspace_id": str(workspace.id)},
        request_id=request_id,
    )
    await session.commit()
    return _auth_response(user, workspace), refresh_token


async def set_development_password(
    session: AsyncSession,
    user_id: uuid.UUID,
    password: str,
    *,
    request_id: str,
    user_agent: str | None,
    ip: str | None,
) -> tuple[AuthResponse, str]:
    if settings.environment != "development":
        raise ApiError(status_code=404, code="HTTP_ERROR", message="Endpoint is unavailable")
    user = await session.get(User, user_id)
    if user is None or user.deleted_at is not None:
        raise ApiError(status_code=404, code="USER_NOT_FOUND", message="User was not found")
    if user.password_hash is not None:
        raise ApiError(status_code=409, code="ENTITY_IN_USE", message="Password is already set")
    user.password_hash = hash_password(password)
    workspace = await _workspace_for_user(session, user.id)
    _, refresh_token = await create_refresh_session(session, user, user_agent=user_agent, ip=ip)
    await record_audit(
        session,
        workspace_id=workspace.id,
        actor_user_id=user.id,
        entity_type="user",
        entity_id=user.id,
        action="update",
        before_data=None,
        after_data={"password_set": True},
        request_id=request_id,
    )
    await session.commit()
    return _auth_response(user, workspace), refresh_token


async def login_user(
    session: AsyncSession,
    email: str,
    password: str,
    *,
    request_id: str,
    user_agent: str | None,
    ip: str | None,
) -> tuple[AuthResponse, str]:
    normalized_email = normalize_email(email)
    if await _rate_key_count(normalized_email, ip) >= MAX_LOGIN_FAILURES:
        raise ApiError(status_code=429, code="ACCOUNT_LOCKED", message="Try again later")
    user = await session.scalar(
        select(User).where(User.normalized_email == normalized_email, User.deleted_at.is_(None))
    )
    now = datetime.now(UTC)
    valid = user is not None and user.is_active and verify_password(user.password_hash, password)
    if not valid:
        failures = await _record_failure(normalized_email, ip)
        if user is not None:
            user.failed_login_attempts += 1
            if failures >= MAX_LOGIN_FAILURES:
                user.locked_until = now + timedelta(minutes=LOCK_MINUTES)
            await session.commit()
        raise ApiError(
            status_code=401,
            code="INVALID_CREDENTIALS",
            message="Email or password is invalid",
        )
    assert user is not None
    if user.locked_until is not None and user.locked_until > now:
        raise ApiError(status_code=429, code="ACCOUNT_LOCKED", message="Try again later")
    await _reset_rate_limit(normalized_email, ip)
    user.failed_login_attempts = 0
    user.locked_until = None
    user.last_login_at = now
    workspace = await _workspace_for_user(session, user.id)
    _, refresh_token = await create_refresh_session(session, user, user_agent=user_agent, ip=ip)
    await record_audit(
        session,
        workspace_id=workspace.id,
        actor_user_id=user.id,
        entity_type="user",
        entity_id=user.id,
        action="user.login",
        before_data=None,
        after_data={"user_id": str(user.id)},
        request_id=request_id,
    )
    await session.commit()
    return _auth_response(user, workspace), refresh_token


def parse_refresh_cookie(value: str | None) -> tuple[uuid.UUID, str]:
    if not value or "." not in value:
        raise ApiError(status_code=401, code="SESSION_EXPIRED", message="Session is missing")
    session_value, secret = value.split(".", 1)
    try:
        return uuid.UUID(session_value), secret
    except ValueError as exc:
        raise ApiError(
            status_code=401, code="SESSION_EXPIRED", message="Session is invalid"
        ) from exc


async def rotate_refresh_session(
    session: AsyncSession,
    cookie: str | None,
    *,
    request_id: str,
    user_agent: str | None,
    ip: str | None,
) -> tuple[AuthResponse, str]:
    session_id, secret = parse_refresh_cookie(cookie)
    old = await session.scalar(
        select(AuthSession).where(AuthSession.id == session_id).with_for_update()
    )
    now = datetime.now(UTC)
    if old is None or old.expires_at <= now:
        raise ApiError(status_code=401, code="SESSION_EXPIRED", message="Session has expired")
    if not constant_hash_match(secret, old.refresh_token_hash):
        raise ApiError(status_code=401, code="SESSION_REVOKED", message="Session is invalid")
    if old.revoked_at is not None:
        if old.replaced_by_session_id is not None:
            await session.execute(
                update(AuthSession)
                .where(AuthSession.user_id == old.user_id, AuthSession.revoked_at.is_(None))
                .values(revoked_at=now)
            )
            workspace = await _workspace_for_user(session, old.user_id)
            await record_audit(
                session,
                workspace_id=workspace.id,
                actor_user_id=old.user_id,
                entity_type="auth_session",
                entity_id=old.id,
                action="auth.session.revoked",
                before_data=None,
                after_data={"reason": "refresh_token_reuse", "all_sessions": True},
                request_id=request_id,
            )
            await session.commit()
            raise ApiError(
                status_code=401,
                code="TOKEN_REUSE_DETECTED",
                message="Refresh token reuse was detected",
            )
        raise ApiError(status_code=401, code="SESSION_REVOKED", message="Session is revoked")
    user = await session.get(User, old.user_id)
    if user is None or not user.is_active:
        raise ApiError(status_code=401, code="SESSION_REVOKED", message="Session is invalid")
    new_session, new_cookie = await create_refresh_session(
        session, user, user_agent=user_agent, ip=ip
    )
    old.revoked_at = now
    old.last_used_at = now
    old.replaced_by_session_id = new_session.id
    workspace = await _workspace_for_user(session, user.id)
    await session.commit()
    return _auth_response(user, workspace), new_cookie


async def user_from_refresh_cookie(session: AsyncSession, cookie: str | None) -> User:
    """Authenticate an OAuth browser callback without rotating its refresh cookie."""
    session_id, secret = parse_refresh_cookie(cookie)
    auth_session = await session.get(AuthSession, session_id)
    now = datetime.now(UTC)
    if (
        auth_session is None
        or auth_session.expires_at <= now
        or auth_session.revoked_at is not None
        or not constant_hash_match(secret, auth_session.refresh_token_hash)
    ):
        raise ApiError(status_code=401, code="SESSION_EXPIRED", message="Session is invalid")
    user = await session.get(User, auth_session.user_id)
    if user is None or not user.is_active or user.deleted_at is not None:
        raise ApiError(status_code=401, code="SESSION_REVOKED", message="Session is invalid")
    auth_session.last_used_at = now
    return user


async def revoke_session(
    session: AsyncSession,
    cookie: str | None,
    *,
    request_id: str,
) -> bool:
    try:
        session_id, secret = parse_refresh_cookie(cookie)
    except ApiError:
        return False
    auth_session = await session.get(AuthSession, session_id)
    if auth_session is None or not constant_hash_match(secret, auth_session.refresh_token_hash):
        return False
    if auth_session.revoked_at is None:
        auth_session.revoked_at = datetime.now(UTC)
    workspace = await _workspace_for_user(session, auth_session.user_id)
    await record_audit(
        session,
        workspace_id=workspace.id,
        actor_user_id=auth_session.user_id,
        entity_type="auth_session",
        entity_id=auth_session.id,
        action="user.logout",
        before_data=None,
        after_data={"session_id": str(auth_session.id)},
        request_id=request_id,
    )
    await session.commit()
    return True


async def revoke_all_sessions(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    request_id: str,
) -> None:
    now = datetime.now(UTC)
    await session.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    workspace = await _workspace_for_user(session, user_id)
    await record_audit(
        session,
        workspace_id=workspace.id,
        actor_user_id=user_id,
        entity_type="user",
        entity_id=user_id,
        action="auth.session.revoked",
        before_data=None,
        after_data={"all_sessions": True},
        request_id=request_id,
    )
    await session.commit()
