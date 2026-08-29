"""交付物数据模型 `deliverables`。

- `git_link` 和 `text` 将内容存入 `content`，`file` 引用 `stored_files` 并可追溯
  `sha256`；
- 版本号在同一工作项内递增，重提不覆盖旧版本；
  `(work_item_id, version)` 唯一约束用于阻止并发创建重复版本。
"""

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.models.base import CoreModel

DELIVERABLE_TYPES = ("git_link", "text", "file")


class Deliverable(CoreModel):
    __tablename__ = "deliverables"
    __table_args__ = (
        CheckConstraint("type IN ('git_link', 'text', 'file')", name="ck_deliverables_type"),
        UniqueConstraint("work_item_id", "version", name="uq_deliverables_item_version"),
    )

    # 项目归属从所属工作项推导，API 不接受客户端传入。
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, nullable=False
    )
    work_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_items.id"), index=True, nullable=False
    )
    type: Mapped[str] = mapped_column(nullable=False)
    # 保存 `git_link` 的 URL 或 `text` 正文；`file` 类型为 `None`。
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    # `file` 类型引用已上传文件，完整性哈希保存在 `stored_files`。
    stored_file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stored_files.id"), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    submitted_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), index=True, nullable=False
    )
