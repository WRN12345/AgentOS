"""0008：创建 deliverables 与 reviews，支持交付物版本化和最终审核。

Revision ID: 0008_deliverables_reviews
Revises: 0007_stored_files
Create Date: 2026-07-27

- deliverables：三类交付物（git_link/text/file）按工作项版本递增留痕；
  (work_item_id, version) 唯一约束防止并发重提产生重复版本，旧版本保留可查；
  file 类型经 stored_file_id 关联 stored_files，可追溯 sha256。
- reviews：负责人最终审核留痕（结论、反馈、被审交付物版本、审核人）。
- collaboration_requests 增加可空 result_deliverable_id / result_file_id：
  协作产物回传可引用交付物或文件。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_deliverables_reviews"
down_revision: str | None = "0007_stored_files"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deliverables",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), primary_key=True),
        sa.Column("work_item_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("work_items.id"), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        # git_link 类型的链接 URL 或 text 类型的正文；file 类型为 NULL
        sa.Column("content", sa.Text(), nullable=True),
        # file 类型引用的已上传文件（可追溯 stored_files.sha256）
        sa.Column("stored_file_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("stored_files.id"), nullable=True),
        # 版本号在同一工作项内从 1 递增，重新提交时不覆盖旧版本
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("submitted_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("project_members.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("type IN ('git_link', 'text', 'file')", name="ck_deliverables_type"),
        sa.UniqueConstraint("work_item_id", "version", name="uq_deliverables_item_version"),
    )
    op.create_index("ix_deliverables_work_item_id", "deliverables", ["work_item_id"])
    op.create_index("ix_deliverables_submitted_by", "deliverables", ["submitted_by"])

    op.create_table(
        "reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), primary_key=True),
        sa.Column("work_item_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("work_items.id"), nullable=False),
        # 被审核的交付物版本
        sa.Column("deliverable_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("deliverables.id"), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        # 反馈正文属于隐私信息，仅负责人与该工作项主执行人可见
        sa.Column("feedback", sa.Text(), nullable=True),
        sa.Column("reviewed_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("project_members.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "decision IN ('approve', 'request_changes', 'reject')", name="ck_reviews_decision"
        ),
    )
    op.create_index("ix_reviews_work_item_id", "reviews", ["work_item_id"])

    # 协作产物回传可引用交付物/文件（可空，文本回传走既有 result_text）
    op.add_column(
        "collaboration_requests",
        sa.Column("result_deliverable_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("deliverables.id"), nullable=True),
    )
    op.add_column(
        "collaboration_requests",
        sa.Column("result_file_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("stored_files.id"), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("collaboration_requests", "result_file_id")
    op.drop_column("collaboration_requests", "result_deliverable_id")
    op.drop_table("reviews")
    op.drop_table("deliverables")
