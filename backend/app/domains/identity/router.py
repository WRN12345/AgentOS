"""身份接口（12.1 节）：登录、刷新、登出、当前用户。"""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.idempotency import idempotency_guard
from app.domains.identity.dependencies import get_current_user
from app.domains.identity.models import User
from app.domains.identity.schemas import (
    LoginRequest,
    LogoutRequest,
    MyProjectOut,
    RefreshRequest,
    TokenPairResponse,
    UserOut,
)
from app.domains.identity.service import login, revoke_refresh_token, rotate_refresh_token
from app.domains.project.models import ProjectMember
from app.infrastructure.database.engine import get_session

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenPairResponse)
async def login_endpoint(
    payload: LoginRequest,
    session: AsyncSession = Depends(get_session),
) -> TokenPairResponse:
    return await login(session, payload.username, payload.password)


@router.post("/refresh", response_model=TokenPairResponse)
async def refresh_endpoint(
    payload: RefreshRequest,
    session: AsyncSession = Depends(get_session),
) -> TokenPairResponse:
    """刷新并轮换 Refresh Token：旧令牌立即作废。"""
    return await rotate_refresh_token(session, payload.refresh_token)


@router.post("/logout")
async def logout_endpoint(
    payload: LogoutRequest,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(idempotency_guard),
) -> dict[str, str]:
    """登出：撤销 Refresh Token。支持 Idempotency-Key（重复登出返回首次结果）。"""
    await revoke_refresh_token(session, payload.refresh_token)
    return {"status": "ok"}


@router.get("/me", response_model=UserOut)
async def me_endpoint(current_user: User = Depends(get_current_user)) -> UserOut:
    return UserOut(
        id=current_user.id,
        username=current_user.username,
        is_active=current_user.is_active,
        is_admin=current_user.is_admin,
        created_at=current_user.created_at,
    )


@router.get("/me/projects", response_model=list[MyProjectOut])
async def my_projects_endpoint(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """返回当前用户参与的所有项目及角色。

    用 get_current_user（仅需登录态，不依赖项目成员身份），
    避免"先有项目上下文才能查项目列表"的鸡生蛋问题。
    """
    stmt = (
        select(ProjectMember)
        .where(ProjectMember.user_id == current_user.id, ProjectMember.is_active == True)
        .order_by(ProjectMember.created_at)
    )
    memberships = (await session.execute(stmt)).scalars().all()

    from app.domains.project.models import Project  # 避免循环导入

    result = []
    for m in memberships:
        project = await session.get(Project, m.project_id)
        if project is None:
            continue
        result.append({
            "id": str(project.id),
            "name": project.name,
            "description": project.description,
            "role": m.role,
        })
    return result
