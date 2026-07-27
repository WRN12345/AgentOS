"""reviews 数据模型（7.5、11 章）：负责人最终审核留痕。

- 关联被审核的交付物版本（deliverable_id）；
- decision：approve（通过并完成）/ request_changes（要求修改，必须填反馈）/
  reject（拒绝当前交付但保持工作项继续执行，7.5 节）；
- feedback 为隐私信息：仅负责人与该工作项主执行人可见（16 节，见 service 层校验）。
"""

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.models.base import CoreModel

REVIEW_DECISIONS = ("approve", "request_changes", "reject")


class Review(CoreModel):
    __tablename__ = "reviews"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('approve', 'request_changes', 'reject')", name="ck_reviews_decision"
        ),
    )

    work_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_items.id"), index=True, nullable=False
    )
    # 被审核的交付物版本
    deliverable_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deliverables.id"), nullable=False
    )
    decision: Mapped[str] = mapped_column(nullable=False)
    # 反馈正文：隐私信息，不进通知、不进全员透明范围（16 节）
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), nullable=False
    )
