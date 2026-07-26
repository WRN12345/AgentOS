"""工作项接口请求/响应模型（12.3 节）。

更新与命令接口均要求携带 version 做乐观锁（17.2 节）：
不匹配返回 409 WORK_ITEM_VERSION_CONFLICT，成功后 version + 1。
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Priority = Literal["low", "medium", "high", "urgent"]


class MemberBrief(BaseModel):
    """成员简况（嵌套在工作项响应中）。"""

    id: uuid.UUID
    display_name: str


class WorkItemCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str = ""
    acceptance_criteria: str | None = None
    priority: Priority = "medium"
    assignee_id: uuid.UUID
    collaborator_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    due_at: datetime | None = None


class WorkItemUpdateIn(BaseModel):
    """负责人修改内容/负责人/DDL/协作者。version 必带；未提供字段保持不变。"""

    version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    acceptance_criteria: str | None = None
    priority: Priority | None = None
    assignee_id: uuid.UUID | None = None
    collaborator_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)
    due_at: datetime | None = None


class WorkItemCommandIn(BaseModel):
    """状态命令（publish/start/block/unblock/submit/cancel）：携带乐观锁版本号。"""

    version: int = Field(ge=1)


class WorkItemOut(BaseModel):
    id: uuid.UUID
    title: str
    description: str
    acceptance_criteria: str | None
    priority: str
    status: str
    assignee: MemberBrief
    collaborators: list[MemberBrief]
    due_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


class WorkItemSummaryOut(BaseModel):
    """列表摘要（13.1 节）：标题、状态、负责人、优先级、DDL、version。"""

    id: uuid.UUID
    title: str
    status: str
    priority: str
    assignee: MemberBrief
    due_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime
