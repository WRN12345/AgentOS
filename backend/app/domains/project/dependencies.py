"""项目成员依赖项：把当前登录用户解析为项目成员，供权限策略使用。

- get_current_member：从 X-Project-Id 请求头取出项目上下文，校验成员身份；
- get_current_admin：校验 users.is_admin（全局角色，不绑定项目）；
- get_current_leader：仅项目负责人；
- get_current_leader_or_admin：负责人或全局管理员（只读管理视图，如审计查询）。

多项目后，admin 升级为全局角色（users.is_admin），不再有项目成员记录。
权限策略的其余部分集中在 domains/project/service.py（4.1 节）。
"""

import uuid

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiException, ErrorCodes
from app.domains.identity.dependencies import get_current_user
from app.domains.identity.models import User
from app.domains.project.models import ROLE_LEADER, ProjectMember
from app.domains.project.service import get_member_by_user
from app.infrastructure.database.engine import get_session


async def get_current_member(
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ProjectMember:
    """解析当前用户在当前项目下的成员身份。

    从 X-Project-Id 请求头读取项目上下文：
    - 缺失或空白 → 400（MISSING_PROJECT_ID）
    - UUID 格式无效 → 400
    - 用户不是该项目成员或成员已禁用 → 403（NOT_PROJECT_MEMBER）
    """
    project_id_str = request.headers.get("X-Project-Id", "").strip()
    if not project_id_str:
        raise ApiException(400, ErrorCodes.MISSING_PROJECT_ID, "缺少项目上下文，请携带 X-Project-Id 请求头")
    try:
        project_id = uuid.UUID(project_id_str)
    except ValueError:
        raise ApiException(
            400, ErrorCodes.MISSING_PROJECT_ID, "X-Project-Id 格式无效，须为合法 UUID"
        ) from None

    member = await get_member_by_user(session, project_id, current_user.id)
    if member is None or not member.is_active:
        raise ApiException(403, ErrorCodes.NOT_PROJECT_MEMBER, "当前账号不是该项目成员或已被禁用")
    return member


async def get_current_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """校验当前用户是否为全局管理员（users.is_admin）。"""
    if not current_user.is_admin:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅全局管理员可执行该操作")
    return current_user


async def get_current_leader(
    member: ProjectMember = Depends(get_current_member),
) -> ProjectMember:
    """仅项目负责人。"""
    if member.role != ROLE_LEADER:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅项目负责人可执行该操作")
    return member


async def get_current_leader_or_admin(
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> User:
    """负责人或全局管理员（管理员可读管理视图，但不参与业务操作）。

    全局管理员直接放行（不依赖项目成员身份）；
    非管理员须在当前项目下持有 leader 角色。
    """
    # 全局管理员：直接放行
    if current_user.is_admin:
        return current_user

    # 非管理员：从 X-Project-Id 解析项目成员身份并校验 leader 角色
    project_id_str = request.headers.get("X-Project-Id", "").strip()
    if not project_id_str:
        raise ApiException(400, ErrorCodes.MISSING_PROJECT_ID, "缺少项目上下文，请携带 X-Project-Id 请求头")
    try:
        project_id = uuid.UUID(project_id_str)
    except ValueError:
        raise ApiException(
            400, ErrorCodes.MISSING_PROJECT_ID, "X-Project-Id 格式无效，须为合法 UUID"
        ) from None

    member = await get_member_by_user(session, project_id, current_user.id)
    if member is None or not member.is_active or member.role != ROLE_LEADER:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅项目负责人或管理员可执行该操作")
    return current_user
