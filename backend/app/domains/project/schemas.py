"""成员接口请求/响应模型（12.2 节）。

响应只含透明工作台所需的摘要字段（6.1 节、原则 6），
绝不包含密码哈希、令牌等敏感字段（16 节）。
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    """负责人添加已有账号成员（16 节，不开放公开注册；建号收敛到 admin）。

    2026-08-17 规则调整：
    - 账号创建由全局管理员负责（admin 控制台建号）；
    - 项目负责人仅「添加已有账号」：按 username（全局唯一）或 user_id 解析已有账号加入本项目，
      不建号、无初始密码，固定为「成员」角色；
    - 每项目仅一名负责人，由 admin 指定/变更，成员接口不再提供角色字段。
    不接受传入 project_id。
    """

    model_config = ConfigDict(extra="forbid")

    user_id: uuid.UUID | None = None
    username: str | None = Field(default=None, min_length=1, max_length=64)
    display_name: str | None = Field(default=None, min_length=1, max_length=64)
    weekly_available_hours: float | None = Field(default=None, ge=0, le=168)
    git_username: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _validate_identity(self) -> "MemberCreateIn":
        # 添加已有账号：username（全局唯一）或 user_id 须且仅须提供其一
        if (self.user_id is None) == (self.username is None):
            raise ValueError("添加已有账号须且仅须提供 username 或 user_id 之一")
        return self


class MemberUpdateIn(BaseModel):
    """负责人维护成员资料 / 禁用启用。未提供的字段保持不变。

    2026-08-17 规则调整：每项目仅一名负责人、由 admin 指定/变更，本接口不再提供 role 字段；
    负责人可维护本项目内任何成员（含负责人本人资料）。不接受传入 project_id。
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=64)
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
