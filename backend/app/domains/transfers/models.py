"""转派申请数据模型（7.3、11 章）：transfer_requests。

- 转派的是工作项主责任（assignee），审批通过前 work_items.assignee_id 不变；
- from_member_id / to_member_id / approved_by 均指向 project_members（多外键指向同表，
  本模型不建 relationship，查询时按需 join，避免消歧复杂度）；
- agent_suggestion_id 为阶段 5 Agent 能力匹配建议预留列（7.3 节），首版恒为空；
- 同一工作项同时最多一条 PENDING：数据库唯一部分索引兜底（迁移 0006，17.2 节）；
- version（VersionMixin）为乐观锁版本号（17.2 节）。
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
    # 当前负责人（发起人）：创建时必须是工作项当前主执行人
    from_member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), index=True, nullable=False
    )
    # 建议的新负责人
    to_member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), index=True, nullable=False
    )
    # 转派原因（7.3 节必填）
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # 对 DDL 和现有协作请求的影响说明（7.3 节必填）
    impact_note: Mapped[str] = mapped_column(Text, nullable=False)
    # Agent 能力匹配建议（阶段 5 填充，首版恒为空）
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
