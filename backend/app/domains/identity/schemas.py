"""身份接口请求/响应模型（12.1 节）。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=512)


class LogoutRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=512)


class TokenPairResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # Access Token 有效期（秒）


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)  # 兼容 ORM 直接校验（如 admin 建号响应构造）

    id: uuid.UUID
    username: str
    is_active: bool
    is_admin: bool
    created_at: datetime


class MyProjectOut(BaseModel):
    """用户参与的项目摘要（GET /me/projects）。"""
    id: uuid.UUID
    name: str
    description: str | None
    role: str  # "leader" | "member"
