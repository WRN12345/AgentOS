"""文件记录数据模型 `stored_files`。

- 数据库仅保存相对 `storage_key` 与存储后端名，不保存宿主机绝对路径；
- `sha256` 用于完整性校验；
- `work_item_id` 可空，文件可独立上传后再关联工作项。
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.models.base import CoreModel


class StoredFile(CoreModel):
    __tablename__ = "stored_files"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, nullable=False
    )
    # 存储后端使用稳定名称，当前实现为 `local`。
    storage_backend: Mapped[str] = mapped_column(String(32), nullable=False, default="local")
    # 后端内相对键必须唯一；Provider 层拒绝绝对路径和 `..`。
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), index=True, nullable=False
    )
    # 文件可先独立上传，再在业务事务中关联工作项。
    work_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_items.id"), index=True, nullable=True
    )
    # 同项目同名文件的 `version` 递增；`superseded_by` 指向替代版本，`NULL` 表示当前版本。
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stored_files.id", deferrable=True, initially="DEFERRED"), nullable=True
    )
    # 索引状态为 `pending → indexing → indexed/failed`；不可读取的格式进入终态 `unindexed`，
    # `failed` 可手动重试回 `pending`。
    index_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # Worker 根据租约开始时间恢复超时任务，避免进程中断或 Redis 重投失败后永久停在 `indexing`。
    index_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
