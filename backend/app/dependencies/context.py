import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy import select

from app.core.config import settings
from app.core.errors import ApiError
from app.core.security import decode_access_token
from app.db.models.users import User, Workspace, WorkspaceMember
from app.dependencies.database import DbSession


@dataclass(frozen=True, slots=True)
class RequestContext:
    user: User
    workspace: Workspace
    role: str
    request_id: str


def _header_uuid(value: str, name: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message=f"{name} must contain a UUID value",
        ) from exc


async def get_request_context(
    request: Request,
    session: DbSession,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    user_header: Annotated[str | None, Header(alias="X-User-ID")] = None,
    workspace_header: Annotated[str | None, Header(alias="X-Workspace-ID")] = None,
) -> RequestContext:
    user_id: uuid.UUID
    if authorization is not None and authorization.lower().startswith("bearer "):
        user_id = decode_access_token(authorization.split(" ", 1)[1].strip())
    elif (
        settings.environment == "development"
        and settings.allow_dev_auth_headers
        and user_header is not None
    ):
        user_id = _header_uuid(user_header, "X-User-ID")
    else:
        raise ApiError(
            status_code=401,
            code="INVALID_CREDENTIALS",
            message="Authentication is required",
        )

    user = await session.scalar(
        select(User).where(
            User.id == user_id,
            User.deleted_at.is_(None),
            User.is_active.is_(True),
        )
    )
    if user is None:
        raise ApiError(
            status_code=401,
            code="INVALID_CREDENTIALS",
            message="Authentication credentials are invalid",
        )

    if workspace_header is not None:
        workspace_id = _header_uuid(workspace_header, "X-Workspace-ID")
        membership_row = (
            await session.execute(
                select(WorkspaceMember, Workspace)
                .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
                .where(
                    WorkspaceMember.user_id == user_id,
                    WorkspaceMember.workspace_id == workspace_id,
                    Workspace.deleted_at.is_(None),
                )
            )
        ).one_or_none()
    else:
        membership_row = (
            await session.execute(
                select(WorkspaceMember, Workspace)
                .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
                .where(
                    WorkspaceMember.user_id == user_id,
                    Workspace.deleted_at.is_(None),
                )
                .order_by(Workspace.created_at, Workspace.id)
                .limit(1)
            )
        ).one_or_none()
    if membership_row is None:
        raise ApiError(
            status_code=403,
            code="WORKSPACE_ACCESS_DENIED",
            message="User does not belong to this workspace",
        )
    member, workspace = membership_row
    return RequestContext(
        user=user,
        workspace=workspace,
        role=member.role,
        request_id=str(getattr(request.state, "request_id", "")),
    )


async def require_workspace_member(
    context: Annotated[RequestContext, Depends(get_request_context)],
) -> RequestContext:
    return context


async def require_workspace_editor(
    context: Annotated[RequestContext, Depends(get_request_context)],
) -> RequestContext:
    if context.role not in {"editor", "owner"}:
        raise ApiError(status_code=403, code="INSUFFICIENT_ROLE", message="Editor role is required")
    return context


async def require_workspace_owner(
    context: Annotated[RequestContext, Depends(get_request_context)],
) -> RequestContext:
    if context.role != "owner":
        raise ApiError(status_code=403, code="INSUFFICIENT_ROLE", message="Owner role is required")
    return context


CurrentContext = Annotated[RequestContext, Depends(require_workspace_member)]
EditorContext = Annotated[RequestContext, Depends(require_workspace_editor)]
OwnerContext = Annotated[RequestContext, Depends(require_workspace_owner)]
