import uuid
from decimal import Decimal

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import Subquery

from app.db.models.goals import Goal, GoalCommandResult, GoalContribution
from app.db.models.users import User

GoalProjectionRow = tuple[Goal, Decimal, int]
ContributionHistoryRow = tuple[GoalContribution, str | None]


def _aggregate_subquery() -> Subquery:
    return (
        select(
            GoalContribution.workspace_id.label("workspace_id"),
            GoalContribution.goal_id.label("goal_id"),
            func.coalesce(func.sum(GoalContribution.amount), 0).label("contributed_amount"),
            func.count(GoalContribution.id).label("contribution_count"),
        )
        .group_by(GoalContribution.workspace_id, GoalContribution.goal_id)
        .subquery()
    )


async def list_goals(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    status: str | None,
    currency: str | None,
    include_deleted: bool,
    search: str | None,
    limit: int,
    offset: int,
) -> tuple[list[GoalProjectionRow], int]:
    filters = [Goal.workspace_id == workspace_id]
    if not include_deleted:
        filters.append(Goal.deleted_at.is_(None))
    if status is not None:
        filters.append(Goal.status == status)
    if currency is not None:
        filters.append(Goal.currency == currency)
    if search:
        pattern = f"%{search.strip()}%"
        filters.append(or_(Goal.name.ilike(pattern), Goal.description.ilike(pattern)))

    total = int(await session.scalar(select(func.count()).select_from(Goal).where(*filters)) or 0)
    aggregate = _aggregate_subquery()
    status_rank = case(
        (Goal.status == "active", 0),
        (Goal.status == "paused", 1),
        (Goal.status == "completed", 2),
        else_=3,
    )
    rows = (
        await session.execute(
            select(
                Goal,
                func.coalesce(aggregate.c.contributed_amount, 0),
                func.coalesce(aggregate.c.contribution_count, 0),
            )
            .outerjoin(
                aggregate,
                (aggregate.c.workspace_id == Goal.workspace_id) & (aggregate.c.goal_id == Goal.id),
            )
            .where(*filters)
            .order_by(
                status_rank,
                Goal.target_date.asc().nulls_last(),
                Goal.created_at.desc(),
                Goal.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [(row[0], Decimal(row[1]), int(row[2])) for row in rows], total


async def get_goal(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    goal_id: uuid.UUID,
    *,
    include_deleted: bool = False,
    for_update: bool = False,
) -> Goal | None:
    filters = [Goal.workspace_id == workspace_id, Goal.id == goal_id]
    if not include_deleted:
        filters.append(Goal.deleted_at.is_(None))
    statement = select(Goal).where(*filters)
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def aggregate_for_goal(
    session: AsyncSession, workspace_id: uuid.UUID, goal_id: uuid.UUID
) -> tuple[Decimal, int]:
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(GoalContribution.amount), 0),
                func.count(GoalContribution.id),
            ).where(
                GoalContribution.workspace_id == workspace_id,
                GoalContribution.goal_id == goal_id,
            )
        )
    ).one()
    return Decimal(row[0]), int(row[1])


async def get_goal_projection(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    goal_id: uuid.UUID,
    *,
    include_deleted: bool = False,
) -> GoalProjectionRow | None:
    goal = await get_goal(
        session, workspace_id, goal_id, include_deleted=include_deleted, for_update=False
    )
    if goal is None:
        return None
    amount, count = await aggregate_for_goal(session, workspace_id, goal_id)
    return goal, amount, count


async def contribution_for_goal(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    goal_id: uuid.UUID,
    contribution_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> GoalContribution | None:
    statement = select(GoalContribution).where(
        GoalContribution.workspace_id == workspace_id,
        GoalContribution.goal_id == goal_id,
        GoalContribution.id == contribution_id,
    )
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def correction_total(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    goal_id: uuid.UUID,
    original_id: uuid.UUID,
) -> Decimal:
    value = await session.scalar(
        select(func.coalesce(func.sum(GoalContribution.amount), 0)).where(
            GoalContribution.workspace_id == workspace_id,
            GoalContribution.goal_id == goal_id,
            GoalContribution.correction_of_id == original_id,
        )
    )
    return Decimal(value or 0)


async def list_contributions(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    goal_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> tuple[list[ContributionHistoryRow], int]:
    filters = (
        GoalContribution.workspace_id == workspace_id,
        GoalContribution.goal_id == goal_id,
    )
    total = int(
        await session.scalar(select(func.count()).select_from(GoalContribution).where(*filters))
        or 0
    )
    rows = (
        await session.execute(
            select(GoalContribution, User.display_name)
            .join(User, User.id == GoalContribution.created_by)
            .where(*filters)
            .order_by(
                GoalContribution.contributed_at.desc(),
                GoalContribution.created_at.desc(),
                GoalContribution.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [(row[0], row[1]) for row in rows], total


async def command_result_for_key(
    session: AsyncSession, workspace_id: uuid.UUID, idempotency_key: str
) -> GoalCommandResult | None:
    return await session.scalar(
        select(GoalCommandResult).where(
            GoalCommandResult.workspace_id == workspace_id,
            GoalCommandResult.idempotency_key == idempotency_key,
        )
    )
