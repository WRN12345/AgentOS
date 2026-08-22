"""memory_chunks 数据模型（设计文档第 5、13 节，迁移 0023）。

- 四类记忆共用一张表，source_type 区分：document / profile / history / core_memory；
- project_id 仅 profile 类型为空（随人走、跨项目放行的唯一例外，16.12）；
- embedding 维度固定 1024（qwen3-embedding:0.6b），换模型按 16.4 全量重建；
- is_current=False 的块不参与检索（旧版本文档内容，仅人工追溯，第 3 节）。
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.infrastructure.models.base import CoreModel

#: 记忆来源类型（与迁移 0023 的 ck_memory_chunks_source_type 一致）
SOURCE_TYPES = ("document", "profile", "history", "core_memory")

#: 核心记忆归属范围（与迁移 0026 的 ck_core_memory_entries_scope 一致）；
#: organization 为组织级预留，本期接口层只接受 project（第 8 节）
CORE_MEMORY_SCOPES = ("project", "organization")
#: 核心记忆条目状态（与迁移 0026 的 ck_core_memory_entries_status 一致）
CORE_MEMORY_STATUSES = ("active", "deprecated")

#: 单项目核心记忆容量预算（生效条目合计字符数，设计文档第 8 节）；
#: 单条上限与预算一致——一条吃掉全部预算也允许，由容量校验兜底
CORE_MEMORY_BUDGET_CHARS = 4000


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


class CoreMemoryEntry(CoreModel):
    """核心记忆条目（设计文档第 8 节，迁移 0026）。

    - 条目式核心笔记（约定/决策/教训），拆解分配时全量注入 Agent 上下文；
    - scope 预留组织级归属，本期仅 project 可选，organization 不挂项目；
    - proposed_by_member_id 为 None 表示 Agent 提议（确认后才生效）；
    - confirmed_by_member_id 必填：负责人确认/手写是生效前提（红线：Agent 不直接改数据）；
    - 作废条目保留供追溯（status=deprecated），不再注入。
    """

    __tablename__ = "core_memory_entries"

    # 项目归属；组织级（预留）为 None
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, nullable=True
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="project")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # 提议者；None = Agent 提议（走建议审批通道，M4.4）
    proposed_by_member_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), nullable=True
    )
    confirmed_by_member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), nullable=False
    )
    effective_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
