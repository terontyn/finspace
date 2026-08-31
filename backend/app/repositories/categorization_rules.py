import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.categorization_rule_sets import CategorizationRuleSetControl
from app.db.models.categorization_rules import CategorizationRule


async def get_or_create_rule_set_control(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    for_update: bool = False,
    for_share: bool = False,
) -> CategorizationRuleSetControl:
    """Return the workspace rule-set control row, creating it on first use.

    Mirrors the month-close control primitive: an insert-on-conflict makes concurrent first use
    safe even though the migration already backfills every existing workspace. ``for_update`` is
    the exclusive gate taken by rule mutations; ``for_share`` is the compatible lock taken by
    categorization applies so that several applies can run at once while mutations wait.
    """
    if for_update and for_share:
        raise ValueError("Rule-set control cannot be locked exclusively and shared at once")
    await session.execute(
        insert(CategorizationRuleSetControl)
        .values(workspace_id=workspace_id, version=1)
        .on_conflict_do_nothing(index_elements=[CategorizationRuleSetControl.workspace_id])
    )
    statement = select(CategorizationRuleSetControl).where(
        CategorizationRuleSetControl.workspace_id == workspace_id
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    elif for_share:
        statement = statement.with_for_update(read=True).execution_options(populate_existing=True)
    control = await session.scalar(statement)
    if control is None:
        raise RuntimeError("Categorization rule-set control row could not be created")
    return control


async def get_rule(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    rule_id: uuid.UUID,
    *,
    include_deleted: bool = False,
    for_update: bool = False,
    for_share: bool = False,
) -> CategorizationRule | None:
    statement = select(CategorizationRule).where(
        CategorizationRule.id == rule_id,
        CategorizationRule.workspace_id == workspace_id,
    )
    if not include_deleted:
        statement = statement.where(CategorizationRule.deleted_at.is_(None))
    if for_update:
        # The rule may already be present in this session's identity map after matching.
        statement = statement.with_for_update().execution_options(populate_existing=True)
    elif for_share:
        statement = statement.with_for_update(read=True).execution_options(populate_existing=True)
    return await session.scalar(statement)


async def list_rules(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    include_deleted: bool,
    is_active: bool | None,
    limit: int,
    offset: int,
) -> tuple[list[CategorizationRule], int]:
    filters = [CategorizationRule.workspace_id == workspace_id]
    if not include_deleted:
        filters.append(CategorizationRule.deleted_at.is_(None))
    if is_active is not None:
        filters.append(CategorizationRule.is_active == is_active)
    total = int(
        await session.scalar(select(func.count()).select_from(CategorizationRule).where(*filters))
        or 0
    )
    items = list(
        (
            await session.scalars(
                select(CategorizationRule)
                .where(*filters)
                .order_by(
                    CategorizationRule.priority,
                    CategorizationRule.created_at,
                    CategorizationRule.id,
                )
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return items, total


async def active_rules(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    refresh: bool = False,
) -> list[CategorizationRule]:
    """Return the deterministic active rule set.

    ``refresh`` forces ``populate_existing`` so rows already in this session's identity map are
    re-read from the database. The confirmation pass of an apply needs it: rules loaded during the
    optimistic preview would otherwise keep their stale attribute values even though the statement
    itself sees the newer snapshot.
    """
    statement = (
        select(CategorizationRule)
        .where(
            CategorizationRule.workspace_id == workspace_id,
            CategorizationRule.deleted_at.is_(None),
            CategorizationRule.is_active.is_(True),
        )
        .order_by(
            CategorizationRule.priority,
            CategorizationRule.created_at,
            CategorizationRule.id,
        )
    )
    if refresh:
        statement = statement.execution_options(populate_existing=True)
    return list((await session.scalars(statement)).all())
