"""管理控制台接口模型。

管理接口只暴露平台管理所需的最小字段：
- 项目摘要仅含 `leader` 成员摘要，不含业务明细；
- 账号信息不包含密码哈希或令牌。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domains.identity.schemas import UserOut


class LeaderBrief(BaseModel):
    """项目负责人摘要。"""

    id: uuid.UUID
    user_id: uuid.UUID
    username: str
    display_name: str


class AdminProjectOut(BaseModel):
    """项目摘要。

    `leader` 为按加入时间排序的首位负责人成员；项目没有负责人时为 `None`。
    """

    id: uuid.UUID
    name: str
    description: str | None
    leader: LeaderBrief | None
    created_at: datetime
    updated_at: datetime


class ProjectCreateIn(BaseModel):
    """全局管理员创建项目时必须指定 `leader` 成员。

    负责人只能是参与项目业务的普通用户。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1000)
    owner_user_id: uuid.UUID


class AdminProjectLeaderIn(BaseModel):
    """全局管理员变更项目唯一负责人。"""

    model_config = ConfigDict(extra="forbid")

    user_id: uuid.UUID


class AdminUserCreateIn(BaseModel):
    """由全局管理员创建账号，系统不开放公开注册。"""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=128)


class AdminUserCreatedOut(UserOut):
    """建号响应：初始密码仅此一次返回，之后不可再查。"""

    initial_password: str


class AdminUserUpdateIn(BaseModel):
    """账号启停状态 `is_active` 同时控制登录和项目业务访问。"""

    model_config = ConfigDict(extra="forbid")

    is_active: bool


__all__ = [
    "AdminProjectLeaderIn",
    "AdminProjectOut",
    "AdminUserCreatedOut",
    "AdminUserCreateIn",
    "AdminUserUpdateIn",
    "LeaderBrief",
    "ProjectCreateIn",
    "UserOut",
]
