"""0023：memory_chunks 表——记忆模块的统一向量块存储（设计文档第 5、13 节）。

Revision ID: 0023_memory_chunks
Revises: 0022_pgvector_extension
Create Date: 2026-08-21

四类记忆（文档/成员档案/历史/核心记忆）共用同一套索引管道与存储，按 source_type 区分：

- project_id 可空：仅成员档案（profile）随人走、不挂项目（16.12 跨项目放行的例外），
  其余类型一律带 project_id 并按项目隔离；
- embedding 维度固定 1024，与 EMBEDDING_DIMENSIONS（qwen3-embedding:0.6b）一致；
  更换模型/维度时按 16.4 全量重建后切换；
- model_version 记录生成向量的模型版本，检索只命中当前版本；
- is_current 标记旧版本文档块的失效（检索只命中最新版本，设计文档第 3 节），
  失效块保留供人工追溯。

向量上暂不建近似索引：MVP 数据量下顺序扫描足够，待数据量增长后再评估 HNSW。
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector

revision: str = "0023_memory_chunks"
down_revision: str | None = "0022_pgvector_extension"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIMENSIONS = 1024


def upgrade() -> None:
    op.create_table(
        "memory_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=False),
        sa.Column("model_version", sa.String(128), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "source_type IN ('document', 'profile', 'history', 'core_memory')",
            name="ck_memory_chunks_source_type",
        ),
        sa.CheckConstraint(
            "source_type = 'profile' OR project_id IS NOT NULL",
            name="ck_memory_chunks_project_required",
        ),
    )
    op.create_index("ix_memory_chunks_project_id", "memory_chunks", ["project_id"])
    op.create_index("ix_memory_chunks_source", "memory_chunks", ["source_type", "source_id"])


def downgrade() -> None:
    op.drop_table("memory_chunks")
