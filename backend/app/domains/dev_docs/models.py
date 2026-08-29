"""开发文档数据模型 `dev_docs`。

- 每个工作项一份，由 `work_item_id` 唯一约束保证；
- author_member_id 可空：负责人对无文档任务执行豁免（waive）时会创建
  占位行，此时还没有撰写人；
- doc_version 记录提交轮次，每次提交递增；
- `waived` 是独立标记，豁免不改变文档状态机，仅放行 `start` 校验；
- `VersionMixin` 提供乐观锁版本号。
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domains.dev_docs.state_machine import DevDocStatus
from app.infrastructure.models.base import CoreModel, VersionMixin


class DevDoc(CoreModel, VersionMixin):
    __tablename__ = "dev_docs"

    work_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_items.id"), unique=True, index=True, nullable=False
    )
    # 豁免占位记录尚无撰写人，因此允许为空。
    author_member_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), nullable=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DevDocStatus.DRAFT.value, index=True
    )
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 每次提交递增，用于展示提交轮次。
    doc_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 负责人豁免后免除 `start` 前置校验，但不改变文档状态。
    waived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    waived_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), nullable=True
    )
    waived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
