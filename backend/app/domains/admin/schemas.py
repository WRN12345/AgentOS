"""管理控制台接口模型（ticket 10）。

管理接口只暴露平台管理所需的最小字段：
- 项目摘要含负责人（leader 成员）摘要，不含任何业务明细；
- 账号只含用户名、启用/管理员标记，绝不包含密码哈希、令牌（16 节）。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.domains.identity.schemas import UserOut


class LeaderBrief(BaseModel):
    """项目负责人（leader 成员）摘要：供管理控制台展示项目归属人。"""

    id: uuid.UUID
    user_id: uuid.UUID
    username: str
    display_name: str


class AdminProjectOut(BaseModel):
    """项目摘要（管理控制台项目列表）。

    leader = 首个负责人成员（按加入时间）；项目尚无负责人时为 null。
    创建项目即指定唯一负责人，故用单数字段；历史/后续若新增负责人，
    前端仍以首个展示（管理控制台"负责人"列）。
    """

    id: uuid.UUID
    name: str
    description: str | None
    leader: LeaderBrief | None
    created_at: datetime
    updated_at: datetime


class ProjectCreateIn(BaseModel):
    """admin 创建项目：必须指定负责人（成为项目的 leader 成员）。

    负责人只能是普通用户（全局管理员不参与项目业务，16 节）。
    """

    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1000)
    owner_user_id: uuid.UUID


class AdminUserUpdateIn(BaseModel):
    """账号管理：启用/禁用（is_active 联动登录与项目业务访问）。"""

    is_active: bool


__all__ = ["AdminProjectOut", "AdminUserUpdateIn", "LeaderBrief", "ProjectCreateIn", "UserOut"]
