"""项目成员依赖项：把当前登录用户解析为项目成员，供权限策略使用。

- get_current_member：任何有效（未禁用）的项目成员；
- get_current_leader：仅项目负责人（6.1 节）。
权限策略的其余部分集中在 domains/project/service.py（4.1 节）。
"""

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiException, ErrorCodes
from app.domains.identity.dependencies import get_current_user
from app.domains.identity.models import User
from app.domains.project.models import ROLE_LEADER, ProjectMember
from app.domains.project.service import get_default_project, get_member_by_user
from app.infrastructure.database.engine import get_session


async def get_current_member(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ProjectMember:
    """解析当前用户的项目成员身份；非成员或已禁用成员一律 403。"""
    project = await get_default_project(session)
    member = await get_member_by_user(session, project.id, current_user.id)
    if member is None or not member.is_active:
        raise ApiException(403, ErrorCodes.NOT_PROJECT_MEMBER, "当前账号不是项目成员或已被禁用")
    return member


async def get_current_leader(
    member: ProjectMember = Depends(get_current_member),
) -> ProjectMember:
    """仅项目负责人。"""
    if member.role != ROLE_LEADER:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅项目负责人可执行该操作")
    return member
