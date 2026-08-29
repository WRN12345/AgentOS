"""负责人最终审核的 reviews 数据模型。

decision 可为 approve、request_changes 或 reject；request_changes 必须填写反馈。
feedback 是受限信息，仅项目负责人与工作项主执行人可见，由 service 层校验。
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
    deliverable_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deliverables.id"), nullable=False
    )
    decision: Mapped[str] = mapped_column(nullable=False)
    # 反馈正文不进入通知，仅限项目负责人与工作项主执行人查看
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), nullable=False
    )
