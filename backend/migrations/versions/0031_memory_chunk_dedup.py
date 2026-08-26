"""0031：memory_chunks 增加块序号与来源级防重唯一约束。

Revision ID: 0031_memory_chunk_dedup
Revises: 0030_file_index_lease
Create Date: 2026-08-26

并发重复索引任务（租约超时重投与原任务并存、多 worker）可能对同一来源
写出两套 current 块，导致检索与问答返回重复依据。唯一约束
(source_type, source_id, model_version, chunk_index) 在数据库层兜底；
worker 侧另有原子认领与 rebuild_chunks 的来源级 advisory 锁串行。

存量数据回填：同一来源同一模型版本内按入库顺序（created_at, id）编号，
既有重复行也会得到不同序号，约束创建不会因历史数据失败。
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0031_memory_chunk_dedup"
down_revision: str | None = "0030_file_index_lease"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memory_chunks",
        sa.Column("chunk_index", sa.Integer(), server_default="0", nullable=False),
    )
    # 回填：同一来源同一模型版本内按入库顺序重新编号（0 起）
    op.execute(
        """
        WITH numbered AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY source_type, source_id, model_version
                       ORDER BY created_at, id
                   ) - 1 AS idx
            FROM memory_chunks
        )
        UPDATE memory_chunks AS mc
        SET chunk_index = n.idx
        FROM numbered AS n
        WHERE mc.id = n.id
        """
    )
    op.alter_column("memory_chunks", "chunk_index", server_default=None)
    op.create_unique_constraint(
        "ux_memory_chunks_source_chunk",
        "memory_chunks",
        ["source_type", "source_id", "model_version", "chunk_index"],
    )


def downgrade() -> None:
    op.drop_constraint("ux_memory_chunks_source_chunk", "memory_chunks")
    op.drop_column("memory_chunks", "chunk_index")
