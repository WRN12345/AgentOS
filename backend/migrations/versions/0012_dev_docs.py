"""0012：开发文档前置（dev_docs，设计文档 2026-07-30 §4.1）。

Revision ID: 0012_dev_docs
Revises: 0011_member_role_admin
Create Date: 2026-07-30

每个工作项一份开发文档（work_item_id 唯一）：主执行人开工前撰写并提交，
负责人确认通过（或豁免）后工作项才允许 READY → IN_PROGRESS。
doc_version 每次提交 +1；历史快照表 dev_doc_versions 为 P2 可选项，本期不建。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_dev_docs"
down_revision: str | None = "0011_member_role_admin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dev_docs",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), primary_key=True),
        sa.Column("work_item_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("work_items.id"), nullable=False),
        # 撰写人（提交时的主执行人）；豁免占位行尚无撰写人，可空
        sa.Column("author_member_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("project_members.id"), nullable=True),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="DRAFT"),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("confirmed_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("project_members.id"), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("doc_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("waived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("waived_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("project_members.id"), nullable=True),
        sa.Column("waived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("work_item_id", name="uq_dev_docs_work_item"),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'SUBMITTED', 'CONFIRMED', 'RETURNED')",
            name="ck_dev_docs_status",
        ),
    )
    op.create_index("ix_dev_docs_work_item_id", "dev_docs", ["work_item_id"])
    op.create_index("ix_dev_docs_status", "dev_docs", ["status"])


def downgrade() -> None:
    op.drop_table("dev_docs")
