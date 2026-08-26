"""0026：core_memory_entries 表——项目核心记忆条目（设计文档第 8 节）。

Revision ID: 0026_core_memory_entries
Revises: 0025_file_index_status
Create Date: 2026-08-22

- 条目式核心笔记：技术约定 / 关键决策 / 踩坑教训，全量注入 Agent 拆解分配上下文；
- scope 预留组织级归属（16 节组织级预留）：本期接口层只接受 project，
  organization 条目不属于任何项目（project_id 为 NULL），表结构不为此返工；
- proposed_by_member_id 可空：NULL 表示 Agent 提议（经负责人确认后生效，第 8 节）；
  confirmed_by_member_id 必填：条目必须经负责人确认（或负责人手写）才生效，
  守住"Agent 不直接改数据"的红线；
- status：active（生效）/ deprecated（作废），作废条目保留供追溯，不再注入。
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0026_core_memory_entries"
down_revision: str | None = "0025_file_index_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "core_memory_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("scope", sa.String(16), nullable=False, server_default="project"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("proposed_by_member_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("project_members.id"), nullable=True),
        sa.Column("confirmed_by_member_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("project_members.id"), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("scope IN ('project', 'organization')", name="ck_core_memory_entries_scope"),
        sa.CheckConstraint("status IN ('active', 'deprecated')", name="ck_core_memory_entries_status"),
        sa.CheckConstraint(
            "scope = 'organization' OR project_id IS NOT NULL",
            name="ck_core_memory_entries_project_required",
        ),
    )
    op.create_index("ix_core_memory_entries_project_id", "core_memory_entries", ["project_id"])


def downgrade() -> None:
    op.drop_table("core_memory_entries")
