import uuid

from fastapi import APIRouter

from app.dependencies.context import CurrentContext, OwnerContext
from app.dependencies.database import DbSession
from app.schemas.users import UserResponse, WorkspaceMemberResponse, WorkspaceResponse
from app.services.workspaces import list_members, remove_member

router = APIRouter()


@router.get("/me", response_model=UserResponse)
async def me(context: CurrentContext) -> UserResponse:
    return UserResponse.model_validate(context.user)


@router.get("/workspaces/current", response_model=WorkspaceResponse)
async def current_workspace(context: CurrentContext) -> WorkspaceResponse:
    return WorkspaceResponse.model_validate(context.workspace)


@router.get("/workspaces/current/members", response_model=list[WorkspaceMemberResponse])
async def current_members(
    context: OwnerContext, session: DbSession
) -> list[WorkspaceMemberResponse]:
    return await list_members(session, context.workspace.id)


@router.delete("/workspaces/current/members/{user_id}", status_code=204)
async def delete_current_member(
    user_id: uuid.UUID,
    context: OwnerContext,
    session: DbSession,
) -> None:
    await remove_member(session, context.workspace.id, user_id)
