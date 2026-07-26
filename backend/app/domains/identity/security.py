"""密码与令牌安全原语（第 16 章）。

- 密码：Argon2id 哈希（argon2-cffi 默认参数）。
- Access Token：JWT（HS256），载荷含用户 ID 与令牌版本 tv。
- Refresh Token：不透明随机串，数据库只存 SHA-256 哈希。

日志纪律：本模块任何函数都不记录密码与令牌原文。
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, VerificationError, VerifyMismatchError

from app.core.config import settings
from app.core.errors import ApiException, ErrorCodes

_password_hasher = PasswordHasher()  # 默认即 Argon2id


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, Argon2Error):
        return False


def create_access_token(user_id: uuid.UUID, token_version: int) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "tv": token_version,
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    """解析并校验 Access Token，失败抛 ApiException（统一错误格式）。"""
    try:
        payload: dict[str, Any] = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except jwt.PyJWTError:
        raise ApiException(401, ErrorCodes.INVALID_TOKEN, "访问令牌无效或已过期") from None
    if payload.get("type") != "access" or "sub" not in payload:
        raise ApiException(401, ErrorCodes.INVALID_TOKEN, "访问令牌无效或已过期")
    return payload


def generate_refresh_token() -> str:
    """生成不透明 Refresh Token（仅此返回值是原文，落库一律用哈希）。"""
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
