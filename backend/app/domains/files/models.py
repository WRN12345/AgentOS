"""stored_files 数据模型（11 章、14 章）。

- 数据库仅保存相对 storage_key 与存储后端名，不保存宿主机绝对路径（14 章）；
- sha256 用于完整性校验与未来对象存储迁移核对（14 章、21.2 节）；
- work_item_id 可空：文件可先独立上传，T4.4 交付物提交时建立/复用关联。
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.models.base import CoreModel


class StoredFile(CoreModel):
    __tablename__ = "stored_files"

    # 项目归属（独立列表入口冗余 project_id，spec D1）
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, nullable=False
    )
    # 存储后端名（local；未来 s3，21.2 节）
    storage_backend: Mapped[str] = mapped_column(String(32), nullable=False, default="local")
    # 后端内相对键，唯一；禁止绝对路径与 ".."（Provider 层强校验）
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), index=True, nullable=False
    )
    # 关联业务对象（11 章）：首版为工作项，可空，T4.4 交付物接入时使用
    work_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_items.id"), index=True, nullable=True
    )
    # 版本链（设计文档第 3 节，迁移 0024）：同项目同名文件 version 递增；
    # superseded_by 指向取代本行的新版本，NULL 即当前最新版本（不可删除，只版本更替）
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stored_files.id", deferrable=True, initially="DEFERRED"), nullable=True
    )
    # 索引状态机（设计文档第 6 节，迁移 0025）：pending → indexing → indexed/failed；
    # 不支持读取内容的格式为 unindexed（终态）；failed 可重试回 pending
    index_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # indexing 租约开始时间。worker 定期恢复超过配置时限的任务，避免进程中断或
    # Redis 重投失败后永久停留在 indexing（迁移 0030）。
    index_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
