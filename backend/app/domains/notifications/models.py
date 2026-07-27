"""站内通知数据模型（2.1、11 章）：notifications。

- 每个关键业务事件由写入服务 notify() 与业务写入同事务落一条通知（不丢）；
- 正文只含摘要信息，不含审核意见等隐私内容（16 节）；
- link 为前端跳转路径（如 /work-items/<id>），可空。
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.models.base import CoreModel


class Notification(CoreModel):
    __tablename__ = "notifications"

    # 接收人（project_members）
    recipient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), index=True, nullable=False
    )
    # 事件类型，复用审计动作名（如 collaboration.requested）
    type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # 前端跳转路径，如 /work-items/<id>
    link: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
