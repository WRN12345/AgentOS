"""DDL 变更申请数据模型 `deadline_change_requests`。

- `target_type` 与 `target_id` 多态指向 `work_items` 或 `collaboration_requests`（无外键，
  应用层校验）；`work_item_id` 关联主工作项，便于查询和实施唯一约束；
- `impact_analysis` 使用可空 `JSONB` 保存规则化分析；`impact_analysis_status`
  以 `generated` 或 `unavailable` 表示分析结果；
- 同一工作项同时最多一条待审批主 DDL 变更，由数据库唯一部分索引兜底；
- `VersionMixin` 提供乐观锁版本号。
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domains.deadlines.state_machine import DeadlineChangeStatus, ImpactAnalysisStatus
from app.infrastructure.models.base import CoreModel, VersionMixin


class DeadlineChangeRequest(CoreModel, VersionMixin):
    __tablename__ = "deadline_change_requests"

    # `work_item` 表示主任务级，`collaboration_request` 表示协作级。
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # 协作级申请也保存所属主工作项，便于统一实施项目隔离。
    work_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_items.id"), index=True, nullable=False
    )
    old_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    new_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # 规则化影响分析失败时为空。
    impact_analysis: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    impact_analysis_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ImpactAnalysisStatus.GENERATED.value
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=DeadlineChangeStatus.PENDING_IMPACT_ANALYSIS.value,
        index=True,
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), index=True, nullable=False
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
