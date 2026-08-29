"""转派申请接口请求与响应模型。

命令接口（approve/reject/cancel）均要求携带 version 进行乐观锁校验：
不匹配返回 409 TRANSFER_VERSION_CONFLICT，成功后 version + 1。
approve/reject 可携带 decision_note，审批意见仅写入审计，不进入通知正文。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.domains.work_items.schemas import MemberBrief


class TransferRequestCreateIn(BaseModel):
    """转派申请仅由当前主执行人发起，原因与影响说明必填。"""

    to_member_id: uuid.UUID
    reason: str = Field(min_length=1)
    impact_note: str = Field(min_length=1)


class TransferCommandIn(BaseModel):
    """状态命令（cancel）：携带乐观锁版本号。"""

    version: int = Field(ge=1)


class TransferDecisionIn(BaseModel):
    """approve/reject 可带审批意见，意见仅写入审计。"""

    version: int = Field(ge=1)
    decision_note: str | None = None


class TransferRequestOut(BaseModel):
    id: uuid.UUID
    work_item_id: uuid.UUID
    work_item_title: str
    from_member: MemberBrief
    to_member: MemberBrief
    reason: str
    impact_note: str
    agent_suggestion_id: uuid.UUID | None
    status: str
    approved_by: MemberBrief | None
    approved_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


class TransferRequestSummaryOut(BaseModel):
    """摘要列表：不含 reason/impact_note 正文。"""

    id: uuid.UUID
    work_item_id: uuid.UUID
    work_item_title: str
    from_member: MemberBrief
    to_member: MemberBrief
    status: str
    version: int
    created_at: datetime
    updated_at: datetime
