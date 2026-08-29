"""0006：转派申请（transfer_requests）与 DDL 变更申请（deadline_change_requests）。

Revision ID: 0006_transfers_deadline_changes
Revises: 0005_collaboration_notifications
Create Date: 2026-07-27

并发约束由数据库部分唯一索引兜底：
- 同一工作项同时最多一条 PENDING 转派申请；
- 同一工作项同时最多一条待审批主 DDL 变更（PENDING_IMPACT_ANALYSIS / PENDING_APPROVAL）。
应用层先做友好检查返回 409，索引在并发窗口下兜底。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_transfers_deadline_changes"
down_revision: str | None = "0005_collaboration_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "transfer_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), primary_key=True),
        sa.Column("work_item_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("work_items.id"), nullable=False),
        sa.Column("from_member_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("project_members.id"), nullable=False),
        sa.Column("to_member_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("project_members.id"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("impact_note", sa.Text(), nullable=False),
        # 转派申请可不依赖 Agent 建议创建，因此关联允许为空
        sa.Column("agent_suggestion_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="PENDING"),
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("project_members.id"), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_transfer_requests_work_item_id", "transfer_requests", ["work_item_id"])
    op.create_index("ix_transfer_requests_from_member_id", "transfer_requests", ["from_member_id"])
    op.create_index("ix_transfer_requests_to_member_id", "transfer_requests", ["to_member_id"])
    op.create_index("ix_transfer_requests_status", "transfer_requests", ["status"])
    # 同一工作项同时只能存在一个待审批转派申请
    op.create_index(
        "uq_transfer_requests_pending_per_item",
        "transfer_requests",
        ["work_item_id"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )

    op.create_table(
        "deadline_change_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), primary_key=True),
        # 目标对象：work_item（主任务级）或 collaboration_request（协作级），多态无外键
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        # 冗余关联主工作项：便于按工作项查询与唯一约束
        sa.Column("work_item_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("work_items.id"), nullable=False),
        sa.Column("old_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("new_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        # 结构化影响分析可能不可用，允许为空以兼容人工审批路径
        sa.Column("impact_analysis", postgresql.JSONB(), nullable=True),
        # generated 表示分析已生成，unavailable 表示分析不可用；分析失败不阻塞人工审批
        sa.Column("impact_analysis_status", sa.String(16), nullable=False, server_default="generated"),
        sa.Column("status", sa.String(32), nullable=False, server_default="PENDING_IMPACT_ANALYSIS"),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("project_members.id"), nullable=False),
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("project_members.id"), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_deadline_change_requests_work_item_id", "deadline_change_requests", ["work_item_id"])
    op.create_index("ix_deadline_change_requests_requested_by", "deadline_change_requests", ["requested_by"])
    op.create_index("ix_deadline_change_requests_status", "deadline_change_requests", ["status"])
    op.create_index("ix_deadline_change_requests_target", "deadline_change_requests", ["target_type", "target_id"])
    # 同一工作项只能有一个待审批主 DDL 变更；协作级变更不受此约束
    op.create_index(
        "uq_deadline_change_requests_pending_main_per_item",
        "deadline_change_requests",
        ["work_item_id"],
        unique=True,
        postgresql_where=sa.text(
            "target_type = 'work_item' AND status IN ('PENDING_IMPACT_ANALYSIS', 'PENDING_APPROVAL')"
        ),
    )


def downgrade() -> None:
    op.drop_table("deadline_change_requests")
    op.drop_table("transfer_requests")
