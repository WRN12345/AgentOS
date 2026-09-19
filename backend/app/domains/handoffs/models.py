"""作为现有工作项输入提供的交付物。"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.models.base import CoreModel, VersionMixin


class DeliverableHandoff(CoreModel, VersionMixin):
    __tablename__ = "deliverable_handoffs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'accepted', 'changes_requested')",
            name="ck_deliverable_handoffs_status",
        ),
        CheckConstraint(
            "source_work_item_id <> target_work_item_id",
            name="ck_deliverable_handoffs_distinct_items",
        ),
        Index(
            "uq_deliverable_handoffs_pending_source",
            "source_work_item_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True
    )
    source_work_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_items.id"), index=True
    )
    target_work_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_items.id"), index=True
    )
    deliverable_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deliverables.id"), index=True
    )
    sender_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), index=True
    )
    recipient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default="pending", server_default="pending"
    )
    note: Mapped[str | None] = mapped_column(Text)
    response_note: Mapped[str | None] = mapped_column(Text)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
