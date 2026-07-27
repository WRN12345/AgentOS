"""协作请求数据模型（7.2、11 章）：collaboration_requests。

- 协作请求由工作项当前主执行人发起，向其他成员索取资料/局部产物；
- 不改变主任务负责人（7.2 节）：任何状态变化都不触碰 work_items.assignee_id；
- requester_id / assignee_id 均指向 project_members（双外键，relationship 需消歧）；
- version（VersionMixin）为乐观锁版本号（17.2 节）。
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
    # 发起人：发起时必须是工作项当前主执行人（7.2 节）
    requester_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), index=True, nullable=False
    )
    # 接收人：接受/拒绝/处理/回传产物的成员
    assignee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), index=True, nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # 独立目标（7.2 节）：协作请求有自己的目标，不从工作项继承
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    # 产物模板文本（可选），引导接收人按模板回传
    template: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 协作 DDL（7.4 节：由发起人与接收人协商，与主任务 DDL 独立）
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 回传产物文本（文件类产物阶段 4 接入）
    result_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CollaborationStatus.REQUESTED.value, index=True
    )
