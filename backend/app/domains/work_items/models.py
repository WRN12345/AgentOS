"""工作项数据模型（7.1、11 章）：work_items 与 work_item_collaborators。

- 主责任唯一（原则 4）：assignee_id 为唯一当前主执行人；
  协作者走 work_item_collaborators 关联表，不承担最终交付责任。
- version（VersionMixin）为乐观锁版本号（17.2 节）。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domains.work_items.state_machine import WorkItemStatus
from app.infrastructure.models.base import CoreModel, VersionMixin

PRIORITIES = ("low", "medium", "high", "urgent")


class WorkItem(CoreModel, VersionMixin):
    __tablename__ = "work_items"
    __table_args__ = (
        CheckConstraint(
            "priority IN ('low', 'medium', 'high', 'urgent')", name="ck_work_items_priority"
        ),
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    acceptance_criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=WorkItemStatus.DRAFT.value, index=True
    )
    # 当前唯一主执行人（原则 4）；历史负责人变化见 audit_events
    assignee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), index=True, nullable=False
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    collaborators: Mapped[list["WorkItemCollaborator"]] = relationship(
        back_populates="work_item",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class WorkItemCollaborator(CoreModel):
    __tablename__ = "work_item_collaborators"
    __table_args__ = (
        UniqueConstraint("work_item_id", "member_id", name="uq_work_item_collaborators_pair"),
    )

    work_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_items.id"), index=True, nullable=False
    )
    member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), nullable=False
    )

    work_item: Mapped[WorkItem] = relationship(back_populates="collaborators")
