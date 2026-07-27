"""deliverables 数据模型（7.5、11 章）。

- 三类交付物：git_link（链接 URL 存 content）、text（正文存 content）、
  file（引用 stored_files，可追溯 sha256，2.1 节）；
- 版本号在同一工作项内从 1 递增，重提不覆盖旧版本（7.5 节）；
  (work_item_id, version) 唯一约束兜底并发重提（17.2 节）。
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

    work_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_items.id"), index=True, nullable=False
    )
    type: Mapped[str] = mapped_column(nullable=False)
    # git_link 的 URL 或 text 的正文；file 类型为 None
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    # file 类型引用的已上传文件（sha256 见 stored_files）
    stored_file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stored_files.id"), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    submitted_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), index=True, nullable=False
    )
