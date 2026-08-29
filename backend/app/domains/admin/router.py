"""管理控制台接口：项目与账号管理。

所有接口均通过 `get_current_admin` 限制为全局管理员访问。全局管理员不属于项目，
也不参与业务协作；审计记录统一通过 `GET /audit-events` 查询。
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.idempotency import idempotency_guard
from app.domains.identity.models import User
from app.domains.project.dependencies import get_current_admin
from app.domains.admin.schemas import (
    AdminProjectLeaderIn,
    AdminProjectOut,
    AdminUserCreatedOut,
    AdminUserCreateIn,
    AdminUserUpdateIn,
    ProjectCreateIn,
    UserOut,
)
from app.domains.admin.service import (
    create_account,
    create_project,
    list_projects,
    list_users,
    update_project_leader,
    update_user,
)
from app.infrastructure.database.engine import get_session

router = APIRouter(tags=["admin"])


@router.get("/projects", response_model=list[AdminProjectOut])
async def list_projects_endpoint(
    _: User = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> list[AdminProjectOut]:
    """项目列表：全部项目及负责人摘要。"""
    return await list_projects(session)


@router.post("/projects", response_model=AdminProjectOut, status_code=201)
async def create_project_endpoint(
    payload: ProjectCreateIn,
    admin: User = Depends(get_current_admin),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> AdminProjectOut:
    """创建项目并指定一名 `leader` 成员。"""
    return await create_project(session, admin, payload)


@router.get("/users", response_model=list[UserOut])
async def list_users_endpoint(
    _: User = Depends(get_current_admin),
    session: AsyncSession = Depends(get_session),
) -> list[User]:
    """账号列表：全部用户账号（不含敏感字段）。"""
    return await list_users(session)


@router.post("/users", response_model=AdminUserCreatedOut, status_code=201)
async def create_user_endpoint(
    payload: AdminUserCreateIn,
    admin: User = Depends(get_current_admin),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> AdminUserCreatedOut:
    """创建全局账号：初始密码仅响应返回一次。"""
    user, initial_password = await create_account(session, admin, payload)
    return AdminUserCreatedOut(
        **UserOut.model_validate(user).model_dump(), initial_password=initial_password
    )


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user_endpoint(
    user_id: uuid.UUID,
    payload: AdminUserUpdateIn,
    admin: User = Depends(get_current_admin),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> User:
    """账号启用/禁用。"""
    return await update_user(session, admin, user_id, payload.is_active)


@router.put("/projects/{project_id}/leader", response_model=AdminProjectOut)
async def update_project_leader_endpoint(
    project_id: uuid.UUID,
    payload: AdminProjectLeaderIn,
    admin: User = Depends(get_current_admin),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> AdminProjectOut:
    """变更项目负责人（每项目仅一名负责人，原负责人降为成员）。"""
    return await update_project_leader(session, admin, project_id, payload.user_id)
