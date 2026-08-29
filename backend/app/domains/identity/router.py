"""登录、令牌刷新、登出和当前用户接口。"""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
    """轮换 `Refresh Token`，并立即撤销旧令牌。"""
    return await rotate_refresh_token(session, payload.refresh_token)


@router.post("/logout")
async def logout_endpoint(
    payload: LogoutRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """幂等撤销 `Refresh Token`，不暴露令牌是否存在。"""
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
    """返回当前用户参与的所有有效项目及角色。

    此接口只依赖 `get_current_user`，无需预先提供项目上下文。
    """
    stmt = (
        select(ProjectMember)
        .where(ProjectMember.user_id == current_user.id, ProjectMember.is_active == True)
        .order_by(ProjectMember.created_at)
    )
    memberships = (await session.execute(stmt)).scalars().all()

    from app.domains.project.models import Project  # 延迟导入以避免循环依赖

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
