"""0009：创建 agent_runs 与 agent_suggestions，记录 Agent 运行和结构化建议。

Revision ID: 0009_agent_runs_suggestions
Revises: 0008_deliverables_reviews
Create Date: 2026-07-27

- agent_runs：一次 Agent 图运行的业务记录——状态流转（pending/running/
  succeeded/failed）、模型、耗时、错误、触发来源、重试次数、request_id；
  LangGraph 检查点只用于中断恢复，不替代本表的业务记录。
- agent_suggestions：结构化建议（JSONB 内容、置信度、风险限制、事实引用、
  prompt 版本）+ 人工采纳结果（pending/accepted/ignored + 反馈时间/人）。

注：LangGraph 检查点表（checkpoints 等）由 AsyncPostgresSaver.setup() 在
worker 首次运行时自建，不归 Alembic 管理。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_agent_runs_suggestions"
down_revision: str | None = "0008_deliverables_reviews"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("agent_type", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("trigger_source", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("work_item_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("work_items.id"), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')", name="ck_agent_runs_status"
        ),
        sa.CheckConstraint(
            "trigger_source IN ('manual', 'scheduler', 'event')",
            name="ck_agent_runs_trigger_source",
        ),
    )
    op.create_index("ix_agent_runs_status", "agent_runs", ["status"])
    op.create_index("ix_agent_runs_agent_type", "agent_runs", ["agent_type"])
    op.create_index("ix_agent_runs_work_item_id", "agent_runs", ["work_item_id"])

    op.create_table(
        "agent_suggestions",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("suggestion_type", sa.String(64), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("risks", sa.Text(), nullable=True),
        sa.Column("fact_refs", postgresql.JSONB(), nullable=True),
        sa.Column("review_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("reviewed_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("project_members.id"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("prompt_version", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "review_status IN ('pending', 'accepted', 'ignored')",
            name="ck_agent_suggestions_review_status",
        ),
    )
    op.create_index("ix_agent_suggestions_run_id", "agent_suggestions", ["run_id"])
    op.create_index("ix_agent_suggestions_suggestion_type", "agent_suggestions", ["suggestion_type"])
    op.create_index("ix_agent_suggestions_review_status", "agent_suggestions", ["review_status"])


def downgrade() -> None:
    op.drop_table("agent_suggestions")
    op.drop_table("agent_runs")
