"""transfer_requests 转派申请数据模型。

- 转派的是工作项主责任（assignee），审批通过前 work_items.assignee_id 不变；
- from_member_id / to_member_id / approved_by 均指向 project_members（多外键指向同表，
  本模型不建 relationship，查询时按需 join，避免消歧复杂度）；
- agent_suggestion_id 保存 Agent 能力匹配建议，可为空；
- 数据库唯一部分索引保证同一工作项最多有一条 PENDING 申请；
- version（VersionMixin）用于乐观锁。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domains.transfers.state_machine import TransferStatus
from app.infrastructure.models.base import CoreModel, VersionMixin


class TransferRequest(CoreModel, VersionMixin):
    __tablename__ = "transfer_requests"

    work_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_items.id"), index=True, nullable=False
    )
    # 创建时的主执行人，也是转派申请发起人
    from_member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), index=True, nullable=False
    )
    to_member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), index=True, nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    impact_note: Mapped[str] = mapped_column(Text, nullable=False)
    agent_suggestion_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=TransferStatus.PENDING.value, index=True
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
