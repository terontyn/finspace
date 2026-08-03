import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models.users import User, WorkspaceMember
from app.schemas.users import WorkspaceMemberResponse


async def list_members(
    session: AsyncSession, workspace_id: uuid.UUID
) -> list[WorkspaceMemberResponse]:
    rows = (
        await session.execute(
            select(WorkspaceMember, User)
            .join(User, User.id == WorkspaceMember.user_id)
            .where(WorkspaceMember.workspace_id == workspace_id, User.deleted_at.is_(None))
            .order_by(User.display_name, User.id)
        )
    ).all()
    return [
        WorkspaceMemberResponse(
            user_id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=member.role,
            created_at=member.created_at,
        )
        for member, user in rows
    ]


async def remove_member(session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID) -> None:
    member = await session.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
        )
    )
    if member is None:
        raise ApiError(status_code=404, code="USER_NOT_FOUND", message="Member was not found")
    if member.role == "owner":
        owners = int(
            await session.scalar(
                select(func.count())
                .select_from(WorkspaceMember)
                .where(
                    WorkspaceMember.workspace_id == workspace_id,
                    WorkspaceMember.role == "owner",
                )
            )
            or 0
        )
        if owners <= 1:
            raise ApiError(
                status_code=409,
                code="ENTITY_IN_USE",
                message="The last workspace owner cannot be removed",
            )
    await session.delete(member)
    await session.commit()
