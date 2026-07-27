"""转派申请接口请求/响应模型（12.4 节）。

命令接口（approve/reject/cancel）均要求携带 version 做乐观锁（17.2 节）：
不匹配返回 409 TRANSFER_VERSION_CONFLICT，成功后 version + 1。
approve/reject 可携带审批意见（decision_note），只进审计留痕，不进通知正文（16 节）。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.domains.work_items.schemas import MemberBrief


class TransferRequestCreateIn(BaseModel):
    """发起转派申请：仅工作项当前主执行人；原因与影响说明必填（7.3 节）。"""

    to_member_id: uuid.UUID
    reason: str = Field(min_length=1)
    impact_note: str = Field(min_length=1)


class TransferCommandIn(BaseModel):
    """状态命令（cancel）：携带乐观锁版本号。"""

    version: int = Field(ge=1)


class TransferDecisionIn(BaseModel):
    """审批命令（approve/reject）：可带审批意见，意见只入审计不进通知（16 节）。"""

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
