"""DDL 变更申请数据模型（7.4、11 章）：deadline_change_requests。

- target_type + target_id 多态指向 work_items 或 collaboration_requests（无外键，
  应用层校验）；work_item_id 冗余关联主工作项，便于按工作项查询与唯一约束；
- impact_analysis（JSONB 可空）存规则化影响分析结果；impact_analysis_status
  （generated/unavailable）预留阶段 5 AI 分析失败时"未生成 AI 影响分析"的表达（8.4 节）；
- 同一工作项同时最多一条待审批主 DDL 变更：数据库唯一部分索引兜底（迁移 0006，17.2 节）；
- version（VersionMixin）为乐观锁版本号（17.2 节）。
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

    # 目标对象类型：work_item（主任务级）/ collaboration_request（协作级）
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # 冗余关联主工作项（7.4 节：协作级也归属于某个主工作项）
    work_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_items.id"), index=True, nullable=False
    )
    old_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    new_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # 规则化影响分析结果（结构化 JSON）；分析失败时为空
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
