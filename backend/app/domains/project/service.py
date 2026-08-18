"""项目应用服务与权限策略（4.1、6.1 节）。

权限规则集中在本模块：每个用例显式校验项目角色与资源关系（16 节）。
审计事件与业务写入同事务（原则 5）：record_event 只 flush，由本服务统一 commit。
日志纪律：不记录密码、令牌（16 节）。
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


# ---------- 查询与权限策略 ----------


async def get_default_project(session: AsyncSession) -> Project:
    """返回首版唯一项目记录（2.2 节不含多项目）。"""
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
        .options(selectinload(ProjectMember.capabilities))  # 预加载，避免异步懒加载
    )
    member = (await session.execute(stmt)).scalar_one_or_none()
    if member is None or member.project_id != project_id:
        # 墙外对象等同不存在（同 work_items 域 get_work_item 的越权 404 模式）
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
    """成员账号管理：仅项目负责人可操作（创建/编辑/禁用成员、维护能力）。
    全局管理员（users.is_admin）不通过成员身份鉴权，由 get_current_admin 依赖单独校验。
    """
    if member.role != ROLE_LEADER:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅项目负责人可执行该操作")


# ---------- 序列化 ----------


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
    """成员摘要序列化：仅透明字段，不含密码哈希、令牌（16 节）。"""
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
    """本项目各成员当前负载：作为主执行人的进行中工作项数量（6.2 节"当前有效任务负载"）。"""
    stmt = (
        select(WorkItem.assignee_id, func.count())
        .where(WorkItem.status.in_(ACTIVE_STATUSES), WorkItem.project_id == project_id)
        .group_by(WorkItem.assignee_id)
    )
    return {row[0]: row[1] for row in (await session.execute(stmt)).all()}


# ---------- 用例 ----------


async def list_members(session: AsyncSession, actor: ProjectMember) -> list[MemberOut]:
    """本项目全员摘要（原则 6：项目内透明）。任何项目成员可查，权限在依赖项校验。"""
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
    """负责人添加已有账号成员（16 节，不开放公开注册）。

    2026-08-17 规则调整：账号创建收敛到 admin（admin 控制台建号），本接口只按 username
    （全局唯一）或 user_id 解析已有账号加入本项目，不建号、无初始密码，固定为「成员」角色
    （每项目仅一名负责人，由 admin 指定/变更）。本函数只建本项目成员，账号建删走 identity/admin 域。
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
    member.capabilities = []  # 显式初始化集合，避免 commit 后访问触发异步懒加载
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
    """负责人维护成员资料 / 项目内禁用启用。

    2026-08-17 规则调整：角色仅由 admin 指定/变更（每项目一名负责人），本接口不再处理 role。
    项目内禁用（is_active）只停本项目成员身份：该账号仍可登录、其他项目照常（get_current_member
    按本项目 member.is_active 门禁 403）；全局禁用（账号无法登录）走 admin 控制台 PATCH /users/{id}。
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
    # 项目内禁用不再联动 users.is_active（2026-08-17 解耦）：账号仍可登录，
    # 全局禁用由 admin 控制台 PATCH /users/{id} 单独管理。

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
        await session.refresh(member)  # updated_at 由数据库 onupdate 生成，刷新取回避免异步懒加载
        logger.info("member updated: member_id=%s, fields=%s", member.id, sorted(after))
    return member_to_out(member, user.username if user else "")


async def put_capabilities(
    session: AsyncSession, actor: ProjectMember, member_id: uuid.UUID, payload: CapabilitiesPutIn
) -> MemberOut:
    """整体替换能力集（PUT 语义，6.2 节）。

    - 成员本人：只能操作自己的能力，提交后 confirmed 复位为未确认；
    - 负责人：可对任意成员操作，confirm=true 时同时确认并留痕。
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
    # 先删除旧能力并 flush：唯一约束 (member_id, tag) 下，同批 flush 会先插后删导致冲突
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
    await session.refresh(member)  # updated_at 由数据库 onupdate 生成，刷新取回避免异步懒加载

    user = await session.get(User, member.user_id)
    return member_to_out(member, user.username if user else "")
