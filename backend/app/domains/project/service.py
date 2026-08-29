"""多项目应用服务与权限策略。

每个用例显式校验项目角色和资源归属，跨项目资源按不存在处理。审计事件通过
`record_event` 仅执行 `flush`，与业务写入在同一事务提交。日志不得记录密码或令牌。
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import ApiException, ErrorCodes
from app.core.logging import setup_logging
from app.domains.audit.service import record_event
from app.domains.identity.models import User
from app.domains.project.models import (
    ROLE_LEADER,
    ROLE_MEMBER,
    MemberCapability,
    Project,
    ProjectMember,
)
from app.domains.project.schemas import (
    CapabilitiesPutIn,
    CapabilityOut,
    MemberCreateIn,
    MemberOut,
    MemberUpdateIn,
)
from app.domains.work_items.models import WorkItem
from app.domains.work_items.state_machine import ACTIVE_STATUSES

logger = setup_logging("backend")


async def get_default_project(session: AsyncSession) -> Project:
    """返回创建时间最早的项目，供未显式指定项目的兼容路径使用。"""
    project = (await session.execute(select(Project).order_by(Project.created_at))).scalars().first()
    if project is None:
        raise ApiException(500, ErrorCodes.INTERNAL_ERROR, "项目尚未初始化，请先执行 bootstrap")
    return project


async def get_member(
    session: AsyncSession, member_id: uuid.UUID, *, project_id: uuid.UUID
) -> ProjectMember:
    stmt = (
        select(ProjectMember)
        .where(ProjectMember.id == member_id)
        .options(selectinload(ProjectMember.capabilities))  # 预加载以避免异步懒加载
    )
    member = (await session.execute(stmt)).scalar_one_or_none()
    if member is None or member.project_id != project_id:
        # 跨项目资源按不存在处理，避免泄露其存在性
        raise ApiException(404, ErrorCodes.NOT_FOUND, "成员不存在")
    return member


async def get_member_by_user(
    session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID
) -> ProjectMember | None:
    stmt = select(ProjectMember).where(
        ProjectMember.project_id == project_id, ProjectMember.user_id == user_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def require_leader(member: ProjectMember) -> None:
    """要求成员具有项目负责人角色。

    全局管理员不通过项目成员身份鉴权，需要由 `get_current_admin` 单独校验。
    """
    if member.role != ROLE_LEADER:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅项目负责人可执行该操作")


def _capability_to_out(cap: MemberCapability) -> CapabilityOut:
    return CapabilityOut(
        id=cap.id,
        tag=cap.tag,
        proficiency=cap.proficiency,
        confirmed=cap.confirmed,
        confirmed_by_member_id=cap.confirmed_by_member_id,
        confirmed_at=cap.confirmed_at,
    )


def member_to_out(
    member: ProjectMember, username: str, active_work_items: int = 0
) -> MemberOut:
    """序列化成员协作摘要，不包含密码哈希或令牌。"""
    return MemberOut(
        id=member.id,
        user_id=member.user_id,
        username=username,
        role=member.role,  # type: ignore[arg-type]
        display_name=member.display_name,
        weekly_available_hours=member.weekly_available_hours,
        git_username=member.git_username,
        is_active=member.is_active,
        active_work_items=active_work_items,
        capabilities=[_capability_to_out(c) for c in member.capabilities],
        created_at=member.created_at,
        updated_at=member.updated_at,
    )


async def _active_work_item_counts(
    session: AsyncSession, project_id: uuid.UUID
) -> dict[uuid.UUID, int]:
    """统计项目内各成员作为主执行人的活跃工作项数量。"""
    stmt = (
        select(WorkItem.assignee_id, func.count())
        .where(WorkItem.status.in_(ACTIVE_STATUSES), WorkItem.project_id == project_id)
        .group_by(WorkItem.assignee_id)
    )
    return {row[0]: row[1] for row in (await session.execute(stmt)).all()}


async def list_members(session: AsyncSession, actor: ProjectMember) -> list[MemberOut]:
    """返回当前项目的全体成员摘要；调用方必须已通过成员身份校验。"""
    stmt = (
        select(ProjectMember)
        .where(ProjectMember.project_id == actor.project_id)
        .order_by(ProjectMember.created_at)
    )
    members = (await session.execute(stmt)).scalars().all()
    counts = await _active_work_item_counts(session, actor.project_id)
    out: list[MemberOut] = []
    for member in members:
        user = await session.get(User, member.user_id)
        out.append(member_to_out(member, user.username if user else "", counts.get(member.id, 0)))
    return out


async def create_member(
    session: AsyncSession, actor: ProjectMember, payload: MemberCreateIn
) -> MemberOut:
    """负责人将已有账号添加到当前项目。

    账号通过全局唯一的 `username` 或 `user_id` 解析。本用例不创建账号，新成员固定为
    `member`；全局管理员账号不能参与项目业务，负责人角色由管理员另行维护。
    """
    require_leader(actor)

    if payload.username is not None:
        user = (
            await session.execute(select(User).where(User.username == payload.username))
        ).scalar_one_or_none()
    else:
        user = await session.get(User, payload.user_id)
    if user is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "账号不存在")
    if user.is_admin:
        raise ApiException(400, ErrorCodes.VALIDATION_ERROR, "全局管理员不参与项目业务，不能加入项目")
    existing = await get_member_by_user(session, actor.project_id, user.id)
    if existing is not None:
        raise ApiException(409, ErrorCodes.VALIDATION_ERROR, "该用户已是本项目成员")

    member = ProjectMember(
        project_id=actor.project_id,
        user_id=user.id,
        role=ROLE_MEMBER,
        display_name=payload.display_name or user.username,
        weekly_available_hours=payload.weekly_available_hours,
        git_username=payload.git_username,
    )
    member.capabilities = []  # 初始化关系，避免提交后访问触发异步懒加载
    session.add(member)
    await session.flush()
    await record_event(
        session,
        actor_id=actor.user_id,
        action="member.created",
        target_type="project_member",
        target_id=member.id,
        before=None,
        after={
            "username": user.username,
            "display_name": member.display_name,
            "role": member.role,
        },
    )
    await session.commit()
    logger.info("member created (existing user): member_id=%s, user_id=%s", member.id, user.id)
    return member_to_out(member, user.username)



async def update_member(
    session: AsyncSession, actor: ProjectMember, member_id: uuid.UUID, payload: MemberUpdateIn
) -> MemberOut:
    """负责人维护成员资料或项目内启用状态。

    此处的 `is_active` 只控制当前项目成员身份，不影响账号登录或其他项目；全局账号状态
    由管理员接口维护。现任负责人不能直接停用，必须先变更项目负责人。
    """
    require_leader(actor)
    member = await get_member(session, member_id, project_id=actor.project_id)

    if member.role == ROLE_LEADER and payload.is_active is False:
        raise ApiException(
            409,
            ErrorCodes.PROJECT_LEADER_REQUIRED,
            "现任负责人不能被禁用，请先由管理员变更项目负责人",
        )

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for field in ("display_name", "weekly_available_hours", "git_username", "is_active"):
        new_value = getattr(payload, field)
        if new_value is None:
            continue
        old_value = getattr(member, field)
        if old_value == new_value:
            continue
        before[field] = old_value
        after[field] = new_value
        setattr(member, field, new_value)

    user = await session.get(User, member.user_id)
    if after:
        await session.flush()
        await record_event(
            session,
            actor_id=actor.user_id,
            action="member.updated",
            target_type="project_member",
            target_id=member.id,
            before=before,
            after=after,
        )
        await session.commit()
        # 异步会话不能隐式重载数据库更新后过期的 `updated_at`
        await session.refresh(member)
        logger.info("member updated: member_id=%s, fields=%s", member.id, sorted(after))
    return member_to_out(member, user.username if user else "")


async def put_capabilities(
    session: AsyncSession, actor: ProjectMember, member_id: uuid.UUID, payload: CapabilitiesPutIn
) -> MemberOut:
    """按 `PUT` 语义整体替换成员能力集。

    普通成员只能修改自己的能力，提交后状态复位为未确认；负责人可以修改任意成员，
    并通过 `confirm=true` 同时确认和记录审计事件。
    """
    can_manage = actor.role == ROLE_LEADER
    if not can_manage and actor.id != member_id:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "只能填报自己的能力，或由负责人维护")
    if payload.confirm and not can_manage:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅项目负责人可确认能力")

    member = await get_member(session, member_id, project_id=actor.project_id)

    before = sorted(f"{c.tag}:{c.proficiency}" for c in member.capabilities)
    after = sorted(f"{c.tag}:{c.proficiency}" for c in payload.capabilities)
    changed = before != after

    confirmed_at = datetime.now(UTC) if payload.confirm else None
    # 先删除并 `flush`，避免同批插入在 `(member_id, tag)` 唯一约束上与旧记录冲突
    member.capabilities = []
    await session.flush()
    member.capabilities = [
        MemberCapability(
            member_id=member.id,
            tag=item.tag,
            proficiency=item.proficiency,
            confirmed=payload.confirm,
            confirmed_by_member_id=actor.id if payload.confirm else None,
            confirmed_at=confirmed_at,
        )
        for item in payload.capabilities
    ]
    await session.flush()

    if changed:
        await record_event(
            session,
            actor_id=actor.user_id,
            action="member.capabilities.submitted",
            target_type="project_member",
            target_id=member.id,
            before={"capabilities": before},
            after={"capabilities": after},
        )
    if payload.confirm:
        await record_event(
            session,
            actor_id=actor.user_id,
            action="member.capabilities.confirmed",
            target_type="project_member",
            target_id=member.id,
            before=None,
            after={"capabilities": after, "confirmed_by_member_id": str(actor.id)},
        )
    await session.commit()
    # 异步会话不能隐式重载数据库更新后过期的 `updated_at`
    await session.refresh(member)

    user = await session.get(User, member.user_id)
    return member_to_out(member, user.username if user else "")
