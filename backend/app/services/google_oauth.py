import base64
import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.encryption import decrypt_google_secret, encrypt_google_secret
from app.core.errors import ApiError
from app.db.models.google_sync import GoogleConnection, GoogleOAuthFlow, GoogleSheetBinding
from app.db.models.users import User, WorkspaceMember
from app.integrations.google_client import GoogleApiError, GoogleClientProtocol
from app.services.audit import record_audit

OAUTH_FLOW_TTL_MINUTES = 10


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _api_error(exc: GoogleApiError) -> ApiError:
    return ApiError(
        status_code=exc.status_code,
        code=exc.code,
        message=str(exc),
    )


async def active_connection(
    session: AsyncSession, workspace_id: uuid.UUID
) -> GoogleConnection | None:
    return await session.scalar(
        select(GoogleConnection).where(
            GoogleConnection.workspace_id == workspace_id,
            GoogleConnection.status == "active",
        )
    )


async def latest_connection(
    session: AsyncSession, workspace_id: uuid.UUID
) -> GoogleConnection | None:
    """Return the latest connection even when it is disconnected or revoked.

    Management screens need the terminal status, while Google API operations must
    continue to use ``active_connection``.
    """
    return await session.scalar(
        select(GoogleConnection)
        .where(GoogleConnection.workspace_id == workspace_id)
        .order_by(GoogleConnection.updated_at.desc(), GoogleConnection.id.desc())
        .limit(1)
    )


async def begin_connect(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> tuple[str, datetime]:
    if not settings.google_is_configured:
        raise ApiError(
            status_code=503,
            code="GOOGLE_NOT_CONFIGURED",
            message="Google OAuth is not configured",
        )
    state = secrets.token_urlsafe(48)
    verifier = secrets.token_urlsafe(64)
    expires_at = datetime.now(UTC) + timedelta(minutes=OAUTH_FLOW_TTL_MINUTES)
    session.add(
        GoogleOAuthFlow(
            state_hash=_hash(state),
            user_id=user_id,
            workspace_id=workspace_id,
            pkce_verifier_encrypted=encrypt_google_secret(verifier, purpose="oauth-pkce"),
            key_version=settings.google_token_encryption_key_version,
            expires_at=expires_at,
        )
    )
    await session.commit()
    query = urlencode(
        {
            "client_id": settings.google_client_id_value,
            "redirect_uri": settings.google_redirect_uri,
            "response_type": "code",
            "scope": " ".join(settings.google_scopes),
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
            "state": state,
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
        }
    )
    return f"https://accounts.google.com/o/oauth2/v2/auth?{query}", expires_at


def _scope_is_granted(required: str, granted: set[str]) -> bool:
    aliases = {
        "email": {"email", "https://www.googleapis.com/auth/userinfo.email"},
        "profile": {"profile", "https://www.googleapis.com/auth/userinfo.profile"},
    }
    return bool(aliases.get(required, {required}) & granted)


async def complete_connect(
    session: AsyncSession,
    client: GoogleClientProtocol,
    *,
    state: str,
    code: str,
    current_user: User,
    request_id: str,
) -> GoogleConnection:
    now = datetime.now(UTC)
    flow = await session.scalar(
        select(GoogleOAuthFlow).where(GoogleOAuthFlow.state_hash == _hash(state)).with_for_update()
    )
    if flow is None or flow.used_at is not None:
        raise ApiError(
            status_code=400, code="CSRF_VALIDATION_FAILED", message="OAuth state is invalid"
        )
    if flow.expires_at <= now:
        flow.used_at = now
        await session.commit()
        raise ApiError(
            status_code=400, code="CSRF_VALIDATION_FAILED", message="OAuth state has expired"
        )
    if flow.user_id != current_user.id:
        raise ApiError(
            status_code=403, code="WORKSPACE_ACCESS_DENIED", message="OAuth user does not match"
        )
    membership = await session.get(WorkspaceMember, (flow.workspace_id, current_user.id))
    if membership is None:
        raise ApiError(
            status_code=403, code="WORKSPACE_ACCESS_DENIED", message="Workspace access was revoked"
        )
    verifier = decrypt_google_secret(flow.pkce_verifier_encrypted, purpose="oauth-pkce")
    flow.used_at = now
    await session.commit()

    try:
        tokens = await client.exchange_code(code, verifier)
        identity = await client.identity(tokens.access_token)
    except GoogleApiError as exc:
        raise _api_error(exc) from exc
    granted = set(tokens.scopes)
    missing = [scope for scope in settings.google_scopes if not _scope_is_granted(scope, granted)]
    if missing:
        raise ApiError(
            status_code=403,
            code="GOOGLE_PERMISSION_DENIED",
            message="Google did not grant all required scopes",
            details={"missing_scopes": missing},
        )

    connection = await active_connection(session, flow.workspace_id)
    if connection is None:
        connection = await session.scalar(
            select(GoogleConnection)
            .where(
                GoogleConnection.workspace_id == flow.workspace_id,
                GoogleConnection.google_subject == identity.subject,
            )
            .order_by(GoogleConnection.updated_at.desc(), GoogleConnection.id.desc())
            .limit(1)
        )
    if connection is None:
        connection = GoogleConnection(
            workspace_id=flow.workspace_id,
            connected_by=current_user.id,
            google_subject=identity.subject,
            token_key_version=settings.google_token_encryption_key_version,
            granted_scopes=sorted(granted),
            status="active",
        )
        session.add(connection)
        await session.flush()
    connection.connected_by = current_user.id
    connection.google_subject = identity.subject
    connection.google_email = identity.email
    connection.access_token_encrypted = encrypt_google_secret(
        tokens.access_token, purpose="google-access-token"
    )
    if tokens.refresh_token:
        connection.refresh_token_encrypted = encrypt_google_secret(
            tokens.refresh_token, purpose="google-refresh-token"
        )
    if connection.refresh_token_encrypted is None:
        raise ApiError(
            status_code=400,
            code="GOOGLE_TOKEN_EXPIRED",
            message="Google did not return an offline refresh token",
        )
    connection.token_key_version = settings.google_token_encryption_key_version
    connection.token_expires_at = tokens.expires_at
    connection.granted_scopes = sorted(granted)
    connection.status = "active"
    connection.last_error_code = None
    connection.last_error_at = None
    connection.revoked_at = None
    connection.updated_at = now
    await record_audit(
        session,
        workspace_id=flow.workspace_id,
        actor_user_id=current_user.id,
        entity_type="google_connection",
        entity_id=connection.id,
        action="google.connect",
        before_data=None,
        after_data={"google_email": identity.email, "scopes": sorted(granted)},
        request_id=request_id,
    )
    await session.commit()
    return connection


async def access_token(
    session: AsyncSession,
    client: GoogleClientProtocol,
    connection: GoogleConnection,
) -> str:
    if connection.status != "active":
        raise ApiError(
            status_code=409, code="GOOGLE_TOKEN_REVOKED", message="Google connection is inactive"
        )
    if connection.access_token_encrypted is None:
        raise ApiError(
            status_code=401, code="GOOGLE_TOKEN_EXPIRED", message="Google access token is missing"
        )
    now = datetime.now(UTC)
    if connection.token_expires_at and connection.token_expires_at > now + timedelta(seconds=60):
        return decrypt_google_secret(
            connection.access_token_encrypted, purpose="google-access-token"
        )
    if connection.refresh_token_encrypted is None:
        connection.status = "expired"
        await session.commit()
        raise ApiError(
            status_code=401, code="GOOGLE_TOKEN_EXPIRED", message="Google refresh token is missing"
        )
    refresh_token = decrypt_google_secret(
        connection.refresh_token_encrypted, purpose="google-refresh-token"
    )
    try:
        tokens = await client.refresh_access_token(refresh_token)
    except GoogleApiError as exc:
        connection.status = "error" if exc.retryable else "revoked"
        connection.last_error_code = exc.code
        connection.last_error_at = now
        await session.commit()
        raise _api_error(exc) from exc
    connection.access_token_encrypted = encrypt_google_secret(
        tokens.access_token, purpose="google-access-token"
    )
    connection.token_expires_at = tokens.expires_at
    connection.updated_at = now
    await session.commit()
    return tokens.access_token


async def disconnect(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    request_id: str,
) -> None:
    connection = await active_connection(session, workspace_id)
    if connection is None:
        raise ApiError(
            status_code=404,
            code="GOOGLE_CONNECTION_NOT_FOUND",
            message="Google connection was not found",
        )
    now = datetime.now(UTC)
    connection.status = "disconnected"
    connection.updated_at = now
    bindings = list(
        (
            await session.scalars(
                select(GoogleSheetBinding).where(
                    GoogleSheetBinding.workspace_id == workspace_id,
                    GoogleSheetBinding.deleted_at.is_(None),
                )
            )
        ).all()
    )
    for binding in bindings:
        binding.status = "disconnected"
        binding.sync_enabled = False
        binding.sync_mode = "paused"
    await record_audit(
        session,
        workspace_id=workspace_id,
        actor_user_id=actor_user_id,
        entity_type="google_connection",
        entity_id=connection.id,
        action="google.disconnect",
        before_data=None,
        after_data={"status": "disconnected"},
        request_id=request_id,
    )
    await session.commit()


async def revoke(
    session: AsyncSession,
    client: GoogleClientProtocol,
    *,
    workspace_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    request_id: str,
) -> None:
    connection = await active_connection(session, workspace_id)
    if connection is None:
        connection = await latest_connection(session, workspace_id)
    if connection is None:
        raise ApiError(
            status_code=404,
            code="GOOGLE_CONNECTION_NOT_FOUND",
            message="Google connection was not found",
        )
    token_blob = connection.refresh_token_encrypted or connection.access_token_encrypted
    if token_blob is not None:
        purpose = (
            "google-refresh-token" if connection.refresh_token_encrypted else "google-access-token"
        )
        try:
            await client.revoke(decrypt_google_secret(token_blob, purpose=purpose))
        except GoogleApiError as exc:
            raise _api_error(exc) from exc
    now = datetime.now(UTC)
    connection.status = "revoked"
    connection.access_token_encrypted = None
    connection.refresh_token_encrypted = None
    connection.revoked_at = now
    connection.updated_at = now
    await session.execute(
        update(GoogleSheetBinding)
        .where(
            GoogleSheetBinding.workspace_id == workspace_id,
            GoogleSheetBinding.deleted_at.is_(None),
        )
        .values(status="disconnected", sync_enabled=False, sync_mode="paused")
    )
    await record_audit(
        session,
        workspace_id=workspace_id,
        actor_user_id=actor_user_id,
        entity_type="google_connection",
        entity_id=connection.id,
        action="google.revoke",
        before_data=None,
        after_data={"status": "revoked"},
        request_id=request_id,
    )
    await session.commit()
