"""身份接口的请求和响应模型。"""

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
    expires_in: int  # `Access Token` 有效期，单位为秒


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)  # 支持直接从 `ORM` 对象构造响应

    id: uuid.UUID
    username: str
    is_active: bool
    is_admin: bool
    created_at: datetime


class MyProjectOut(BaseModel):
    """用户参与的项目摘要。"""
    id: uuid.UUID
    name: str
    description: str | None
    role: str
