from fastapi import APIRouter, Query, Request
from fastapi.responses import RedirectResponse

from app.core.config import settings
from app.core.errors import ApiError
from app.dependencies.context import CurrentContext, OwnerContext
from app.dependencies.database import DbSession
from app.dependencies.google import GoogleClient
from app.schemas.google import GoogleActionResponse, GoogleConnectionStatus, GoogleConnectResponse
from app.services import auth as auth_service
from app.services import google_oauth as service

router = APIRouter()


@router.get("/status", response_model=GoogleConnectionStatus)
async def status(context: CurrentContext, session: DbSession) -> GoogleConnectionStatus:
    connection = await service.latest_connection(session, context.workspace.id)
    return GoogleConnectionStatus(
        configured=settings.google_is_configured,
        connected=connection is not None and connection.status == "active",
        status=connection.status if connection else None,
        google_email=connection.google_email if connection else None,
        granted_scopes=connection.granted_scopes if connection else [],
        token_expires_at=connection.token_expires_at if connection else None,
    )


@router.post("/connect", response_model=GoogleConnectResponse)
async def connect(context: OwnerContext, session: DbSession) -> GoogleConnectResponse:
    authorization_url, expires_at = await service.begin_connect(
        session,
        user_id=context.user.id,
        workspace_id=context.workspace.id,
    )
    return GoogleConnectResponse(authorization_url=authorization_url, expires_at=expires_at)


@router.get("/callback", response_model=None)
async def callback(
    request: Request,
    session: DbSession,
    client: GoogleClient,
    state: str | None = Query(default=None),
    code: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> RedirectResponse:
    if error:
        raise ApiError(
            status_code=400,
            code="GOOGLE_PERMISSION_DENIED",
            message="Google authorization was cancelled",
        )
    if not state or not code:
        raise ApiError(
            status_code=400, code="CSRF_VALIDATION_FAILED", message="OAuth callback is incomplete"
        )
    user = await auth_service.user_from_refresh_cookie(
        session, request.cookies.get(settings.auth_cookie_name)
    )
    await service.complete_connect(
        session,
        client,
        state=state,
        code=code,
        current_user=user,
        request_id=str(getattr(request.state, "request_id", "")),
    )
    frontend = settings.allowed_cors_origins[0].rstrip("/")
    return RedirectResponse(f"{frontend}/?google=connected", status_code=303)


@router.post("/disconnect", response_model=GoogleActionResponse)
async def disconnect(context: OwnerContext, session: DbSession) -> GoogleActionResponse:
    await service.disconnect(
        session,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        request_id=context.request_id,
    )
    return GoogleActionResponse(status="disconnected")


@router.post("/revoke", response_model=GoogleActionResponse)
async def revoke(
    context: OwnerContext,
    session: DbSession,
    client: GoogleClient,
) -> GoogleActionResponse:
    await service.revoke(
        session,
        client,
        workspace_id=context.workspace.id,
        actor_user_id=context.user.id,
        request_id=context.request_id,
    )
    return GoogleActionResponse(status="revoked")
