"""解析项目上下文，并提供项目成员与全局管理员的鉴权依赖。

项目上下文来自 `X-Project-Id`。全局管理员由 `users.is_admin` 标识，不要求存在
`project_members` 记录；管理员绕过成员身份的依赖仅用于明确的只读或管理接口。
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


def project_id_from_request(request: Request) -> uuid.UUID:
    """从 `X-Project-Id` 请求头解析项目上下文。

    请求头缺失、空白或不是合法 `UUID` 时返回 400。
    """
    project_id_str = request.headers.get("X-Project-Id", "").strip()
    if not project_id_str:
        raise ApiException(400, ErrorCodes.MISSING_PROJECT_ID, "缺少项目上下文，请携带 X-Project-Id 请求头")
    try:
        return uuid.UUID(project_id_str)
    except ValueError:
        raise ApiException(
            400, ErrorCodes.MISSING_PROJECT_ID, "X-Project-Id 格式无效，须为合法 UUID"
        ) from None


async def get_current_member(
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ProjectMember:
    """解析当前用户在当前项目中的有效成员身份。

    项目上下文缺失或无效时返回 400；用户不是该项目成员或成员已停用时返回 403。
    """
    member = await get_member_by_user(session, project_id_from_request(request), current_user.id)
    if member is None or not member.is_active:
        raise ApiException(403, ErrorCodes.NOT_PROJECT_MEMBER, "当前账号不是该项目成员或已被禁用")
    return member


async def get_member_or_readonly_admin(
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> tuple[ProjectMember | None, bool]:
    """鉴权有效成员或具有监督读取权限的全局管理员。

    返回 `(member, is_admin)`。非成员或已停用用户若不是全局管理员则返回 403；
    无成员身份的全局管理员返回 `(None, True)`，调用方只能将其用于只读查看。
    """
    member = await get_member_by_user(session, project_id_from_request(request), current_user.id)
    if member is not None and member.is_active:
        return member, bool(current_user.is_admin)
    if not current_user.is_admin:
        raise ApiException(403, ErrorCodes.NOT_PROJECT_MEMBER, "当前账号不是该项目成员或已被禁用")
    return None, True


async def get_current_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """校验当前用户是否为全局管理员。"""
    if not current_user.is_admin:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅全局管理员可执行该操作")
    return current_user


async def get_current_leader(
    member: ProjectMember = Depends(get_current_member),
) -> ProjectMember:
    """校验当前成员是否为项目负责人。"""
    if member.role != ROLE_LEADER:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅项目负责人可执行该操作")
    return member


async def get_current_leader_or_admin(
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> User:
    """校验当前用户是否为项目负责人或全局管理员。

    全局管理员无需项目成员身份；其他用户必须是当前项目的有效 `leader`。
    """
    if current_user.is_admin:
        return current_user

    member = await get_member_by_user(session, project_id_from_request(request), current_user.id)
    if member is None or not member.is_active or member.role != ROLE_LEADER:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅项目负责人或管理员可执行该操作")
    return current_user
