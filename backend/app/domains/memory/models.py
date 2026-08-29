"""记忆块、核心记忆、成员档案和问答历史数据模型。

四类记忆通过 `source_type` 共用 `memory_chunks`。只有 `profile` 不绑定项目；
`embedding` 维度由配置决定，更换模型后必须重建索引。`is_current=False` 的块仅供追溯。
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import settings
from app.infrastructure.models.base import CoreModel

#: 取值必须与数据库约束 `ck_memory_chunks_source_type` 一致。
SOURCE_TYPES = ("document", "profile", "history", "core_memory")

#: `organization` 为组织级预留，当前接口只接受 `project`。
CORE_MEMORY_SCOPES = ("project", "organization")
#: 取值必须与数据库约束 `ck_core_memory_entries_status` 一致，避免应用写入约束外状态。
CORE_MEMORY_STATUSES = ("active", "deprecated")

#: 每个项目的生效条目字符预算；单条内容允许占满全部预算。
CORE_MEMORY_BUDGET_CHARS = 4000

#: 达到此容量比例后，`Agent` 可以提议整合精简。
CORE_MEMORY_NEAR_FULL_RATIO = 0.9


class MemoryChunk(CoreModel):
    __tablename__ = "memory_chunks"
    # 唯一约束与来源级 `advisory lock` 共同防止并发任务写出重复块
    __table_args__ = (
        UniqueConstraint(
            "source_type",
            "source_id",
            "model_version",
            "chunk_index",
            name="ux_memory_chunks_source_chunk",
        ),
    )

    # `profile` 归属于用户而非项目，其他来源必须绑定项目
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, nullable=True
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # 来源实体类型不同，`source_id` 不设置统一外键
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    chunk_index: Mapped[int] = mapped_column(nullable=False, default=0)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(
        Vector(settings.embedding_dimensions), nullable=False
    )
    # 检索只使用当前 `EMBEDDING_MODEL` 生成的向量
    model_version: Mapped[str] = mapped_column(String(128), nullable=False)
    # 失效块保留供追溯，但不参与检索
    is_current: Mapped[bool] = mapped_column(nullable=False, default=True)


class CoreMemoryEntry(CoreModel):
    """需要负责人确认后才会生效的核心记忆条目。

    `scope` 预留组织级归属，当前仅使用 `project`。`proposed_by_member_id` 为空表示
    `Agent` 提议；`confirmed_by_member_id` 必填，确保 `Agent` 不能直接修改生效数据。
    作废条目继续保留，但不再注入上下文。
    """

    __tablename__ = "core_memory_entries"

    # 预留的组织级条目不绑定项目
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, nullable=True
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="project")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    # `None` 表示由 `Agent` 提议并经过审批
    proposed_by_member_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), nullable=True
    )
    confirmed_by_member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), nullable=False
    )
    effective_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MemberProfile(CoreModel):
    """归属于用户、可跨项目读取的成员文字档案。

    档案以 `users.id` 为键，由项目负责人维护，对项目成员和被评价者本人公开。
    停用状态不在本表冗余，停用用户的档案仍保留，但不应进入分配候选。
    """

    __tablename__ = "member_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), unique=True, nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), nullable=False
    )
    last_edited_by_member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), nullable=False
    )

class QaHistory(CoreModel):
    """仅提问者本人可读的知识库问答历史。

    记录问题、结论及依据或线索的 `JSONB` 快照，只追加且无删除接口。
    该表是用户自己的使用记录，不属于审计事件。
    """

    __tablename__ = "qa_history"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, nullable=False
    )
    member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), index=True, nullable=False
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 保存依据或线索快照，避免来源后续变更影响历史记录
    sources: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
