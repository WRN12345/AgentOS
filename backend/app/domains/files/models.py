"""stored_files 数据模型（11 章、14 章）。

- 数据库仅保存相对 storage_key 与存储后端名，不保存宿主机绝对路径（14 章）；
- sha256 用于完整性校验与未来对象存储迁移核对（14 章、21.2 节）；
- work_item_id 可空：文件可先独立上传，T4.4 交付物提交时建立/复用关联。
"""

import uuid

from sqlalchemy import BigInteger, ForeignKey, String
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
