"""项目成员接口的请求和响应模型。

响应只包含项目协作所需的透明摘要，不包含密码哈希或令牌等敏感字段。
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MemberRole = Literal["leader", "member"]


class CapabilityIn(BaseModel):
    """单条能力填报项。"""

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
    active_work_items: int
    capabilities: list[CapabilityOut]
    created_at: datetime
    updated_at: datetime


class MemberCreateIn(BaseModel):
    """负责人将已有账号添加为项目成员。

    通过全局唯一的 `username` 或 `user_id` 指定账号，二者必须且只能提供一个。
    此接口不创建账号、不接受 `project_id`，新成员角色固定为 `member`；负责人由
    全局管理员另行指定或变更。
    """

    model_config = ConfigDict(extra="forbid")

    user_id: uuid.UUID | None = None
    username: str | None = Field(default=None, min_length=1, max_length=64)
    display_name: str | None = Field(default=None, min_length=1, max_length=64)
    weekly_available_hours: float | None = Field(default=None, ge=0, le=168)
    git_username: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _validate_identity(self) -> "MemberCreateIn":
        # `username` 与 `user_id` 必须且只能提供一个，避免身份歧义
        if (self.user_id is None) == (self.username is None):
            raise ValueError("添加已有账号须且仅须提供 username 或 user_id 之一")
        return self


class MemberUpdateIn(BaseModel):
    """负责人维护成员资料或项目内状态，未提供的字段保持不变。

    此接口不接受 `project_id` 或角色变更；负责人由全局管理员另行指定或变更。
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=64)
    weekly_available_hours: float | None = Field(default=None, ge=0, le=168)
    git_username: str | None = Field(default=None, max_length=64)
    is_active: bool | None = None


class CapabilitiesPutIn(BaseModel):
    """按 `PUT` 语义整体替换成员能力集。

    成员本人提交后能力状态复位为未确认；负责人可维护任意成员，并可通过
    `confirm=true` 同时确认。
    """

    capabilities: list[CapabilityIn] = Field(max_length=50)
    confirm: bool = False
