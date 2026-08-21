"""memory_chunks 数据模型（设计文档第 5、13 节，迁移 0023）。

- 四类记忆共用一张表，source_type 区分：document / profile / history / core_memory；
- project_id 仅 profile 类型为空（随人走、跨项目放行的唯一例外，16.12）；
- embedding 维度固定 1024（qwen3-embedding:0.6b），换模型按 16.4 全量重建；
- is_current=False 的块不参与检索（旧版本文档内容，仅人工追溯，第 3 节）。
"""

import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.infrastructure.models.base import CoreModel

#: 记忆来源类型（与迁移 0023 的 ck_memory_chunks_source_type 一致）
SOURCE_TYPES = ("document", "profile", "history", "core_memory")


class MemoryChunk(CoreModel):
    __tablename__ = "memory_chunks"

    # 项目归属；profile 类型为 None（成员档案全局共享，16.12）
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, nullable=True
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # 来源实体 ID（stored_files / member_profiles / agent_runs 等），不做外键——四类来源异构
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(
        Vector(settings.embedding_dimensions), nullable=False
    )
    # 生成向量的模型版本（16.4）：检索只命中当前 EMBEDDING_MODEL 对应的版本
    model_version: Mapped[str] = mapped_column(String(128), nullable=False)
    # 旧版本文档的块标记 False，检索过滤，仅人工追溯（第 3 节）
    is_current: Mapped[bool] = mapped_column(nullable=False, default=True)
