"""审核接口请求与响应模型。

decision 支持三种结论：
- approve：通过并完成工作项（IN_REVIEW → COMPLETED）；
- request_changes：要求修改（IN_REVIEW → IN_PROGRESS），必须填反馈；
- reject：拒绝当前交付，工作项保持 IN_REVIEW。
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.domains.work_items.schemas import MemberBrief


class ReviewCreateIn(BaseModel):
    """提交审核结论：deliverable_id 指定被审核的交付物版本。"""

    deliverable_id: uuid.UUID
    decision: Literal["approve", "request_changes", "reject"]
    feedback: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _check_feedback(self) -> "ReviewCreateIn":
        if self.decision == "request_changes" and not self.feedback:
            raise ValueError("要求修改时必须填写反馈")
        return self


class ReviewOut(BaseModel):
    id: uuid.UUID
    work_item_id: uuid.UUID
    deliverable_id: uuid.UUID
    deliverable_version: int
    decision: str
    feedback: str | None
    reviewed_by: MemberBrief
    work_item_status: str
    created_at: datetime
    updated_at: datetime
