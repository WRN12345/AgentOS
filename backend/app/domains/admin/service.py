"""管理控制台应用服务（ticket 10）。

管理动作是全局平台操作：创建项目时同步建立负责人成员身份，
账号启停联动 users.is_active（禁用即无法登录与访问业务，16 节）。
审计事件与业务写入同事务（原则 5）。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiException, ErrorCodes
from app.core.logging import setup_logging
from app.domains.audit.service import record_event
from app.domains.identity.models import User
from app.domains.project.models import ROLE_LEADER, Project, ProjectMember
from app.domains.admin.schemas import (
    AdminProjectOut,
    LeaderBrief,
    ProjectCreateIn,
)

logger = setup_logging("backend")


async def list_projects(session: AsyncSession) -> list[AdminProjectOut]:
    """全部项目及各自负责人摘要（按创建时间升序）。

    leader = 每个项目下加入最早的 role=leader 成员（管理控制台"负责人"列）。
    """
    projects = (
        await session.execute(select(Project).order_by(Project.created_at))
    ).scalars().all()

    if not projects:
        return []

    # 一次取全部 leader 成员，取每个项目加入最早的作为负责人
    leaders_stmt = (
        select(ProjectMember)
        .where(ProjectMember.role == ROLE_LEADER)
        .order_by(ProjectMember.created_at)
    )
    leader_rows = (await session.execute(leaders_stmt)).scalars().all()

    first_leader_by_project: dict[uuid.UUID, ProjectMember] = {}
    for m in leader_rows:
        if m.project_id not in first_leader_by_project:
            first_leader_by_project[m.project_id] = m

    users_by_id: dict[uuid.UUID, User] = {}
    if first_leader_by_project:
        user_ids = {m.user_id for m in first_leader_by_project.values()}
        users = (
            await session.execute(select(User).where(User.id.in_(user_ids)))
        ).scalars().all()
        users_by_id = {u.id: u for u in users}

    out: list[AdminProjectOut] = []
    for p in projects:
        leader_member = first_leader_by_project.get(p.id)
        leader = None
        if leader_member is not None:
            user = users_by_id.get(leader_member.user_id)
            leader = LeaderBrief(
                id=leader_member.id,
                user_id=leader_member.user_id,
                username=user.username if user else "",
                display_name=leader_member.display_name,
            )
        out.append(
            AdminProjectOut(
                id=p.id,
                name=p.name,
                description=p.description,
                leader=leader,
                created_at=p.created_at,
                updated_at=p.updated_at,
            )
        )
    return out


async def create_project(
    session: AsyncSession, admin: User, payload: ProjectCreateIn
) -> AdminProjectOut:
    """admin 创建项目并指定负责人：同一事务内建项目 + 负责人成员 + 审计。

    负责人只能是普通用户（全局管理员不参与项目业务，16 节）；
    指定已禁用账号 → 400。
    """
    owner = await session.get(User, payload.owner_user_id)
    if owner is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "指定负责人账号不存在")
    if owner.is_admin:
        raise ApiException(
            400, ErrorCodes.VALIDATION_ERROR, "全局管理员不参与项目业务，不能指定为负责人"
        )
    if not owner.is_active:
        raise ApiException(
            400, ErrorCodes.VALIDATION_ERROR, "负责人账号已被禁用，请先启用再指定"
        )

    project = Project(name=payload.name, description=payload.description)
    session.add(project)
    await session.flush()

    leader = ProjectMember(
        project_id=project.id,
        user_id=owner.id,
        role=ROLE_LEADER,
        display_name=owner.username,
    )
    leader.capabilities = []  # 显式初始化集合，避免 commit 后访问触发异步懒加载
    session.add(leader)
    await session.flush()

    await record_event(
        session,
        actor_id=admin.id,
        action="project.created",
        target_type="project",
        target_id=project.id,
        after={"name": project.name, "owner_user_id": str(owner.id)},
    )
    await session.commit()
    await session.refresh(project)
    logger.info("project created: project_id=%s, owner_user_id=%s", project.id, owner.id)
    return AdminProjectOut(
        id=project.id,
        name=project.name,
        description=project.description,
        leader=LeaderBrief(
            id=leader.id,
            user_id=owner.id,
            username=owner.username,
            display_name=leader.display_name,
        ),
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


async def list_users(session: AsyncSession) -> list[User]:
    """全部账号（不含敏感字段，响应模型过滤）。"""
    stmt = select(User).order_by(User.created_at)
    return list((await session.execute(stmt)).scalars().all())


async def update_user(
    session: AsyncSession, admin: User, user_id: uuid.UUID, is_active: bool
) -> User:
    """账号启用/禁用：禁用后立即无法登录且无法访问业务接口。

    禁止禁用当前登录的管理员自己（避免锁死管理入口）。
    """
    user = await session.get(User, user_id)
    if user is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "账号不存在")
    if user.id == admin.id and not is_active:
        raise ApiException(
            400, ErrorCodes.VALIDATION_ERROR, "不能禁用当前登录的管理员账号"
        )
    if user.is_active == is_active:
        return user

    before = {"is_active": user.is_active}
    user.is_active = is_active
    await session.flush()
    await record_event(
        session,
        actor_id=admin.id,
        action="user.updated",
        target_type="user",
        target_id=user.id,
        before=before,
        after={"is_active": user.is_active},
    )
    await session.commit()
    logger.info("user updated: user_id=%s, is_active=%s", user.id, user.is_active)
    return user
