from fastapi import APIRouter, Request, Response

from app.core.config import settings
from app.core.errors import ApiError
from app.dependencies.context import CurrentContext
from app.dependencies.database import DbSession
from app.schemas.auth import (
    AuthMeResponse,
    AuthResponse,
    LoginRequest,
    LogoutResponse,
    RegisterRequest,
    SetDevelopmentPasswordRequest,
)
from app.schemas.users import UserResponse, WorkspaceResponse
from app.services import auth as service

router = APIRouter()


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


def _verify_origin(request: Request) -> None:
    origin = request.headers.get("Origin")
    if origin is not None and origin not in settings.allowed_cors_origins:
        raise ApiError(
            status_code=403,
            code="CSRF_VALIDATION_FAILED",
            message="Request origin is not allowed",
        )


def _set_refresh_cookie(response: Response, value: str) -> None:
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=value,
        max_age=settings.refresh_token_ttl_days * 86400,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
        path="/",
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.auth_cookie_name,
        path="/",
        secure=settings.auth_cookie_secure,
        httponly=True,
        samesite=settings.auth_cookie_samesite,
    )


@router.post("/register", response_model=AuthResponse, status_code=201)
async def register(
    data: RegisterRequest,
    request: Request,
    response: Response,
    session: DbSession,
) -> AuthResponse:
    _verify_origin(request)
    result, refresh_token = await service.register_user(
        session,
        data,
        request_id=str(getattr(request.state, "request_id", "")),
        user_agent=request.headers.get("User-Agent"),
        ip=_client_ip(request),
    )
    _set_refresh_cookie(response, refresh_token)
    return result


@router.post("/login", response_model=AuthResponse)
async def login(
    data: LoginRequest,
    request: Request,
    response: Response,
    session: DbSession,
) -> AuthResponse:
    _verify_origin(request)
    result, refresh_token = await service.login_user(
        session,
        data.email,
        data.password,
        request_id=str(getattr(request.state, "request_id", "")),
        user_agent=request.headers.get("User-Agent"),
        ip=_client_ip(request),
    )
    _set_refresh_cookie(response, refresh_token)
    return result


@router.post("/refresh", response_model=AuthResponse)
async def refresh(request: Request, response: Response, session: DbSession) -> AuthResponse:
    _verify_origin(request)
    result, refresh_token = await service.rotate_refresh_session(
        session,
        request.cookies.get(settings.auth_cookie_name),
        request_id=str(getattr(request.state, "request_id", "")),
        user_agent=request.headers.get("User-Agent"),
        ip=_client_ip(request),
    )
    _set_refresh_cookie(response, refresh_token)
    return result


@router.post("/logout", response_model=LogoutResponse)
async def logout(request: Request, response: Response, session: DbSession) -> LogoutResponse:
    _verify_origin(request)
    revoked = await service.revoke_session(
        session,
        request.cookies.get(settings.auth_cookie_name),
        request_id=str(getattr(request.state, "request_id", "")),
    )
    _clear_refresh_cookie(response)
    return LogoutResponse(revoked=revoked)


@router.post("/logout-all", response_model=LogoutResponse)
async def logout_all(
    request: Request,
    response: Response,
    context: CurrentContext,
    session: DbSession,
) -> LogoutResponse:
    _verify_origin(request)
    await service.revoke_all_sessions(
        session,
        context.user.id,
        request_id=context.request_id,
    )
    _clear_refresh_cookie(response)
    return LogoutResponse(revoked=True)


@router.get("/me", response_model=AuthMeResponse)
async def auth_me(context: CurrentContext) -> AuthMeResponse:
    return AuthMeResponse(
        user=UserResponse.model_validate(context.user),
        workspace=WorkspaceResponse.model_validate(context.workspace),
        role=context.role,
    )


@router.post("/set-development-password", response_model=AuthResponse)
async def set_development_password(
    data: SetDevelopmentPasswordRequest,
    request: Request,
    response: Response,
    session: DbSession,
) -> AuthResponse:
    _verify_origin(request)
    result, refresh_token = await service.set_development_password(
        session,
        data.user_id,
        data.password,
        request_id=str(getattr(request.state, "request_id", "")),
        user_agent=request.headers.get("User-Agent"),
        ip=_client_ip(request),
    )
    _set_refresh_cookie(response, refresh_token)
    return result
