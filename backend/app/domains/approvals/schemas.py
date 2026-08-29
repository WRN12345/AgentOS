"""负责人审批聚合接口的统一响应模型。

`kind` 区分事项来源，其专有字段不适用时为 `None`。
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.domains.work_items.schemas import MemberBrief


class ApprovalItemOut(BaseModel):
    """不同审批事项共用的响应结构。

    `GET /approvals` 按 `created_at` 倒序，`GET /approvals/processed` 按
    `updated_at` 倒序且包含 `approved_by` 和 `approved_at`。
    """

    kind: Literal["transfer", "deadline_change", "dev_doc", "delivery_review"]
    id: uuid.UUID
    work_item_id: uuid.UUID
    work_item_title: str
    summary: str  # 转派展示双方，DDL 变更展示目标标题和新旧截止时间。
    requested_by: MemberBrief
    status: str
    impact_analysis_status: str | None  # 仅 `deadline_change` 使用。
    version: int
    created_at: datetime
    updated_at: datetime

    # 待审批列表不包含处理人和处理时间。
    approved_by: MemberBrief | None = None
    approved_at: datetime | None = None

    # `transfer` 专有字段。
    from_member: MemberBrief | None = None
    to_member: MemberBrief | None = None

    # `deadline_change` 专有字段。
    target_type: str | None = None
    target_id: uuid.UUID | None = None
    old_due_at: datetime | None = None
    new_due_at: datetime | None = None

    # `dev_doc` 专有字段：提交次数和打回理由。
    doc_version: int | None = None
    review_note: str | None = None

    # `delivery_review` 专有字段。`status` 承载审核结论；为保护隐私，不聚合反馈正文。
    deliverable_version: int | None = None
    deliverable_type: str | None = None
