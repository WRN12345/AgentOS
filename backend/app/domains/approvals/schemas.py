"""负责人待审批聚合（12.6 节）：GET /approvals 统一响应形状。

PENDING 的转派申请与 PENDING_APPROVAL 的 DDL 变更申请合并为一个列表，
kind 区分来源；kind 专有字段不适用时为 null，前端按 kind 渲染审批卡片。
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.domains.work_items.schemas import MemberBrief


class ApprovalItemOut(BaseModel):
    """统一审批项：转派申请与 DDL 变更申请共用形状，按 created_at 倒序。"""

    kind: Literal["transfer", "deadline_change"]
    id: uuid.UUID
    work_item_id: uuid.UUID
    work_item_title: str
    summary: str  # 目标摘要（转派：from → to；DDL：目标标题 + 新旧 DDL）
    requested_by: MemberBrief
    status: str
    impact_analysis_status: str | None  # 仅 deadline_change 有值；transfer 恒为 null
    version: int
    created_at: datetime
    updated_at: datetime

    # kind=transfer 专有字段（否则为 null）
    from_member: MemberBrief | None = None
    to_member: MemberBrief | None = None

    # kind=deadline_change 专有字段（否则为 null）
    target_type: str | None = None
    target_id: uuid.UUID | None = None
    old_due_at: datetime | None = None
    new_due_at: datetime | None = None
