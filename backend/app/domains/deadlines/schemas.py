"""DDL 变更申请接口的请求与响应模型。

状态命令均携带 `version` 实施乐观锁；版本不匹配返回
`409 DEADLINE_CHANGE_VERSION_CONFLICT`，成功后版本递增。审批意见 `decision_note`
仅进入审计记录，不进入通知正文。
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.domains.work_items.schemas import MemberBrief

DeadlineTarget = Literal["work_item", "collaboration_request"]


class DeadlineChangeCreateIn(BaseModel):
    """发起 DDL 变更申请。

    - `target_type=work_item`：主任务级，一律由负责人审批，`target_id` 须为路径中的工作项；
    - `target_type=collaboration_request`：协作级，`target_id` 为协作请求，
      由协作双方（发起人或接收人）发起。
    """

    target_type: DeadlineTarget
    target_id: uuid.UUID
    new_due_at: datetime
    reason: str = Field(min_length=1)


class DeadlineChangeCommandIn(BaseModel):
    """携带乐观锁版本号的取消命令。"""

    version: int = Field(ge=1)


class DeadlineChangeDecisionIn(BaseModel):
    """审批意见仅进入审计记录，不进入通知正文。"""

    version: int = Field(ge=1)
    decision_note: str | None = None


class DeadlineChangeRequestOut(BaseModel):
    id: uuid.UUID
    work_item_id: uuid.UUID
    work_item_title: str
    target_type: str
    target_id: uuid.UUID
    target_title: str
    old_due_at: datetime | None
    new_due_at: datetime
    reason: str
    impact_analysis: dict[str, Any] | None
    impact_analysis_status: str
    status: str
    requested_by: MemberBrief
    approved_by: MemberBrief | None
    approved_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


class DeadlineChangeSummaryOut(BaseModel):
    """摘要列表：不含 reason 与 impact_analysis 正文。"""

    id: uuid.UUID
    work_item_id: uuid.UUID
    work_item_title: str
    target_type: str
    target_id: uuid.UUID
    target_title: str
    old_due_at: datetime | None
    new_due_at: datetime
    impact_analysis_status: str
    status: str
    requested_by: MemberBrief
    version: int
    created_at: datetime
    updated_at: datetime
