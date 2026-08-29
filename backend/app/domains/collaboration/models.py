"""协作请求数据模型 `collaboration_requests`。

- 协作请求由工作项当前主执行人发起，向其他成员索取资料/局部产物；
- 任何状态变化都不修改 `work_items.assignee_id`；
- `requester_id` 和 `assignee_id` 均指向 `project_members`；
- `VersionMixin` 提供乐观锁版本号。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domains.collaboration.state_machine import CollaborationStatus
from app.infrastructure.models.base import CoreModel, VersionMixin


class CollaborationRequest(CoreModel, VersionMixin):
    __tablename__ = "collaboration_requests"

    work_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_items.id"), index=True, nullable=False
    )
    # 发起时必须是工作项当前主执行人。
    requester_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), index=True, nullable=False
    )
    assignee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # 协作请求使用独立目标，不继承工作项目标。
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    template: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 协作截止时间由双方协商，与主任务截止时间相互独立。
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 可引用本工作项的交付物版本或已上传文件，与 `result_text` 互补。
    result_deliverable_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deliverables.id"), nullable=True
    )
    result_file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stored_files.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CollaborationStatus.REQUESTED.value, index=True
    )
