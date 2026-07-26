"""身份依赖项：解析当前登录用户，供后续所有接口使用。"""

import uuid

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.core.errors import ApiException, ErrorCodes
from app.domains.identity.models import User
from app.domains.identity.security import decode_access_token
from app.domains.identity.service import get_user_by_id
from app.infrastructure.database.engine import get_session


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> User:
    """校验 Bearer Access Token 并返回当前用户。

    失效条件：令牌无效/过期、用户不存在或被禁用、令牌版本落后于
    users.token_version（提升版本即全员旧令牌失效，16 节）。
    解析成功后在 request.state.user_id 登记用户 ID（幂等守卫等下游使用）。
    """
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise ApiException(401, ErrorCodes.INVALID_TOKEN, "缺少访问令牌")
    payload = decode_access_token(authorization.removeprefix("Bearer ").strip())

    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        raise ApiException(401, ErrorCodes.INVALID_TOKEN, "访问令牌无效或已过期") from None

    user = await get_user_by_id(session, user_id)
    if user is None:
        raise ApiException(401, ErrorCodes.INVALID_TOKEN, "访问令牌无效或已过期")
    if not user.is_active:
        raise ApiException(403, ErrorCodes.USER_DISABLED, "账号已被禁用")
    if user.token_version != payload.get("tv"):
        raise ApiException(401, ErrorCodes.INVALID_TOKEN, "访问令牌已失效，请重新登录")

    request.state.user_id = user.id
    return user
