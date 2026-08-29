"""管理控制台应用服务。

管理动作是全局平台操作：创建项目时同步建立负责人成员身份，
账号启停联动 `users.is_active`，禁用后无法登录或访问业务。
审计事件与对应业务变更在同一事务内提交。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiException, ErrorCodes
from app.core.logging import setup_logging
from app.domains.audit.service import record_event
from app.domains.identity.models import User
from app.domains.identity.service import create_user
from app.domains.project.models import ROLE_LEADER, ROLE_MEMBER, Project, ProjectMember
from app.domains.admin.schemas import (
    AdminProjectOut,
    AdminUserCreateIn,
    LeaderBrief,
    ProjectCreateIn,
)

logger = setup_logging("backend")


async def _require_eligible_user(
    session: AsyncSession, user_id: uuid.UUID
) -> User:
    """要求目标账号存在、已启用且不是全局管理员。"""
    user = await session.get(User, user_id)
    if user is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "账号不存在")
    if user.is_admin:
        raise ApiException(
            400, ErrorCodes.VALIDATION_ERROR, "全局管理员不参与项目业务，不能指定为负责人"
        )
    if not user.is_active:
        raise ApiException(
            400, ErrorCodes.VALIDATION_ERROR, "账号已被禁用，请先启用再指定为负责人"
        )
    return user


async def list_projects(session: AsyncSession) -> list[AdminProjectOut]:
    """按创建时间升序返回项目及其最早加入的 `leader` 成员。"""
    projects = (
        await session.execute(select(Project).order_by(Project.created_at))
    ).scalars().all()

    if not projects:
        return []

    # 批量加载后按既定顺序保留每个项目最早加入的 `leader`，避免逐项目查询。
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
    """在同一事务内创建项目、`leader` 成员和审计记录。

    负责人必须是已启用的普通用户。
    """
    owner = await _require_eligible_user(session, payload.owner_user_id)

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
        project_id=project.id,
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
        project_id=None,
    )
    await session.commit()
    logger.info("user updated: user_id=%s, is_active=%s", user.id, user.is_active)
    return user


async def create_account(
    session: AsyncSession, admin: User, payload: AdminUserCreateIn
) -> tuple[User, str]:
    """由全局管理员创建账号，系统不开放公开注册。

    用户名全局唯一：重名 → 409（跨项目同名也在此拦截）。初始密码仅响应返回一次。
    """
    existing = (
        await session.execute(select(User).where(User.username == payload.username))
    ).scalar_one_or_none()
    if existing is not None:
        raise ApiException(409, ErrorCodes.USERNAME_TAKEN, "用户名已被占用")

    user = await create_user(session, payload.username, payload.password)
    await record_event(
        session,
        actor_id=admin.id,
        action="user.created",
        target_type="user",
        target_id=user.id,
        before=None,
        after={"username": user.username, "is_active": user.is_active},
        project_id=None,
    )
    await session.commit()
    logger.info("user created by admin: user_id=%s, username=%s", user.id, user.username)
    return user, payload.password


async def update_project_leader(
    session: AsyncSession, admin: User, project_id: uuid.UUID, user_id: uuid.UUID
) -> AdminProjectOut:
    """由全局管理员变更项目唯一负责人。

    目标账号必须存在、已启用且不是全局管理员。已有项目成员会提升为 `leader`，
    否则创建对应成员；原负责人降为普通成员并保留成员资格。
    """
    project = await session.get(Project, project_id)
    if project is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "项目不存在")
    target = await _require_eligible_user(session, user_id)

    # 将历史遗留的多个 `leader` 一并降级，确保最终只有目标账号担任负责人。
    leaders = (
        await session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.role == ROLE_LEADER,
            )
        )
    ).scalars().all()
    for lm in leaders:
        if lm.user_id != target.id:
            lm.role = ROLE_MEMBER

    target_member = (
        await session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == target.id,
            )
        )
    ).scalar_one_or_none()
    if target_member is None:
        target_member = ProjectMember(
            project_id=project_id,
            user_id=target.id,
            role=ROLE_LEADER,
            display_name=target.username,
        )
        target_member.capabilities = []  # 显式初始化集合，避免 commit 后访问触发异步懒加载
        session.add(target_member)
    else:
        if not target_member.is_active:
            raise ApiException(
                409,
                ErrorCodes.PROJECT_MEMBER_DISABLED,
                "目标成员在本项目已被禁用，请先显式启用后再指定为负责人",
            )
        target_member.role = ROLE_LEADER

    await session.flush()
    await record_event(
        session,
        actor_id=admin.id,
        action="project.leader.updated",
        target_type="project",
        target_id=project.id,
        before={"leader_user_id": str(leaders[0].user_id) if leaders else None},
        after={"leader_user_id": str(target.id)},
        project_id=project.id,
    )
    await session.commit()
    logger.info(
        "project leader updated: project_id=%s, leader_user_id=%s",
        project.id,
        target.id,
    )
    return AdminProjectOut(
        id=project.id,
        name=project.name,
        description=project.description,
        leader=LeaderBrief(
            id=target_member.id,
            user_id=target.id,
            username=target.username,
            display_name=target_member.display_name,
        ),
        created_at=project.created_at,
        updated_at=project.updated_at,
    )
