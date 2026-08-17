"""身份应用服务：登录、刷新、登出与建号（12.1 节）。

会话事务约定：authenticate/create_user 只 flush 不 commit；
login/rotate_refresh_token/revoke_refresh_token 是完整用例，内部自行 commit。
日志纪律：只记录用户名与用户 ID，绝不记录密码、令牌原文。
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ApiException, ErrorCodes
from app.core.logging import setup_logging
from app.domains.identity.models import RefreshToken, User
from app.domains.identity.schemas import TokenPairResponse
from app.domains.identity.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)

logger = setup_logging("backend")


async def create_user(
    session: AsyncSession, username: str, password: str, is_active: bool = True
) -> User:
    """创建账号（只 flush，由调用方统一 commit）。首版不开放公开注册（16 节）。"""
    user = User(
        username=username,
        password_hash=hash_password(password),
        is_active=is_active,
    )
    session.add(user)
    await session.flush()
    return user


async def authenticate(session: AsyncSession, username: str, password: str) -> User:
    """校验用户名密码；错误密码与不存在用户返回同一错误，避免账号枚举。"""
    user = (await session.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if user is None or not verify_password(user.password_hash, password):
        raise ApiException(401, ErrorCodes.INVALID_CREDENTIALS, "用户名或密码错误")
    if not user.is_active:
        raise ApiException(403, ErrorCodes.USER_DISABLED, "账号已被禁用")
    return user


async def _issue_token_pair(session: AsyncSession, user: User) -> TokenPairResponse:
    """签发 Access/Refresh Token 对（只 flush，由调用方统一 commit）。"""
    plain_refresh = generate_refresh_token()
    session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_refresh_token(plain_refresh),
            expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days),
        )
    )
    await session.flush()
    return TokenPairResponse(
        access_token=create_access_token(user.id, user.token_version),
        refresh_token=plain_refresh,
        expires_in=settings.access_token_expire_minutes * 60,
    )


async def login(session: AsyncSession, username: str, password: str) -> TokenPairResponse:
    user = await authenticate(session, username, password)
    pair = await _issue_token_pair(session, user)
    await session.commit()
    logger.info("user logged in: username=%s", username)
    return pair


async def _get_valid_refresh_token(session: AsyncSession, refresh_token: str) -> RefreshToken:
    record = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(refresh_token))
        )
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if record is None or record.revoked_at is not None or record.expires_at <= now:
        raise ApiException(401, ErrorCodes.REFRESH_TOKEN_INVALID, "Refresh Token 无效或已过期")
    return record


async def rotate_refresh_token(session: AsyncSession, refresh_token: str) -> TokenPairResponse:
    """刷新并轮换：旧 Refresh Token 立即撤销，签发新令牌对。"""
    record = await _get_valid_refresh_token(session, refresh_token)
    user = await session.get(User, record.user_id)
    if user is None or not user.is_active:
        raise ApiException(401, ErrorCodes.REFRESH_TOKEN_INVALID, "Refresh Token 无效或已过期")
    record.revoked_at = datetime.now(UTC)
    pair = await _issue_token_pair(session, user)
    await session.commit()
    logger.info("refresh token rotated: user_id=%s", user.id)
    return pair


async def revoke_refresh_token(session: AsyncSession, refresh_token: str) -> None:
    """登出：幂等撤销 Refresh Token；未知/已撤销令牌同样成功，避免令牌探测。"""
    record = (
        await session.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == hash_refresh_token(refresh_token)
            )
        )
    ).scalar_one_or_none()
    if record is None or record.revoked_at is not None:
        return
    record.revoked_at = datetime.now(UTC)
    await session.commit()
    logger.info("refresh token revoked: user_id=%s", record.user_id)


async def get_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)
