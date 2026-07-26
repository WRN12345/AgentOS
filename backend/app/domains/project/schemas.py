"""成员接口请求/响应模型（12.2 节）。

响应只含透明工作台所需的摘要字段（6.1 节、原则 6），
绝不包含密码哈希、令牌等敏感字段（16 节）。
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

MemberRole = Literal["leader", "member"]


class CapabilityIn(BaseModel):
    """单条能力填报项（6.2 节）。"""

    tag: str = Field(min_length=1, max_length=64)
    proficiency: int = Field(ge=1, le=5)


class CapabilityOut(BaseModel):
    id: uuid.UUID
    tag: str
    proficiency: int
    confirmed: bool
    confirmed_by_member_id: uuid.UUID | None
    confirmed_at: datetime | None


class MemberOut(BaseModel):
    """成员摘要：全员透明范围（含能力、负载相关字段）。"""

    id: uuid.UUID
    user_id: uuid.UUID
    username: str
    role: MemberRole
    display_name: str
    weekly_available_hours: float | None
    git_username: str | None
    is_active: bool
    active_work_items: int  # 当前负载：作为主执行人的进行中工作项数量
    capabilities: list[CapabilityOut]
    created_at: datetime
    updated_at: datetime


class MemberCreateIn(BaseModel):
    """负责人创建成员：同时生成登录账号（16 节，不开放公开注册）。"""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=64)
    role: MemberRole = "member"
    weekly_available_hours: float | None = Field(default=None, ge=0, le=168)
    git_username: str | None = Field(default=None, max_length=64)


class MemberCreatedOut(MemberOut):
    """创建响应：初始密码仅此一次返回。"""

    initial_password: str


class MemberUpdateIn(BaseModel):
    """负责人维护成员资料 / 禁用启用。未提供的字段保持不变。"""

    display_name: str | None = Field(default=None, min_length=1, max_length=64)
    role: MemberRole | None = None
    weekly_available_hours: float | None = Field(default=None, ge=0, le=168)
    git_username: str | None = Field(default=None, max_length=64)
    is_active: bool | None = None


class CapabilitiesPutIn(BaseModel):
    """整体替换成员能力集（PUT 语义）。

    - 成员本人：只能提交，提交后 confirmed 复位为未确认；
    - 负责人：可对任意成员操作，confirm=true 时同时确认（6.2 节）。
    """

    capabilities: list[CapabilityIn] = Field(max_length=50)
    confirm: bool = False
