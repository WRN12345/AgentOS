"""开发文档数据模型（设计文档 2026-07-30 §4.1）：dev_docs。

- 每个工作项一份（work_item_id 唯一）：主执行人撰写，协作者/负责人可读；
- author_member_id 可空：负责人对无文档任务执行豁免（waive）时会创建
  占位行，此时还没有撰写人；
- doc_version 每次提交 +1（历史快照 dev_doc_versions 为 P2 可选项，本期不做）；
- waived 是独立标记而非状态：豁免不改变文档状态机，仅放行 start 校验；
- version（VersionMixin）为乐观锁版本号（17.2 节）。
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
    # 撰写人（提交时的主执行人）；豁免占位行尚无撰写人，可空
    author_member_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), nullable=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DevDocStatus.DRAFT.value, index=True
    )
    # 负责人打回理由（可空）
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 每次提交 +1（审批中心展示"第 N 次提交"）
    doc_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 豁免标记：负责人豁免后该任务免于 start 前置校验（独立标记，不改变状态机）
    waived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    waived_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), nullable=True
    )
    waived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
