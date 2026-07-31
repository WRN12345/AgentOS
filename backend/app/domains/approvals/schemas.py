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
    """统一审批项：转派申请与 DDL 变更申请共用形状。

    GET /approvals 按 created_at 倒序（待审批）；GET /approvals/processed
    按 updated_at 倒序（已处理，approved_by/approved_at 有值）。
    """

    kind: Literal["transfer", "deadline_change", "dev_doc", "delivery_review"]
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

    # 处理结果（仅已处理列表有值；待审批列表恒为 null）
    approved_by: MemberBrief | None = None
    approved_at: datetime | None = None

    # kind=transfer 专有字段（否则为 null）
    from_member: MemberBrief | None = None
    to_member: MemberBrief | None = None

    # kind=deadline_change 专有字段（否则为 null）
    target_type: str | None = None
    target_id: uuid.UUID | None = None
    old_due_at: datetime | None = None
    new_due_at: datetime | None = None

    # kind=dev_doc 专有字段（否则为 null）：第 N 次提交 / 打回理由
    doc_version: int | None = None
    review_note: str | None = None

    # kind=delivery_review 专有字段（否则为 null）：被审核的交付物版本与类型；
    # status 承载审核结论（approve/request_changes/reject）。
    # 反馈正文属隐私信息（16 节），不进审批聚合。
    deliverable_version: int | None = None
    deliverable_type: str | None = None
