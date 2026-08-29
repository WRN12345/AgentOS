"""0029：qa_history 表——知识库问答历史。

Revision ID: 0029_qa_history
Revises: 0028_member_profiles
Create Date: 2026-08-24

- 按人落库问答记录（问题、结论、依据快照），仅提问者本人可查——
  负责人/admin 也看不到他人的提问历史；
- 审计域仍不记录提问，避免产生“提问被审计”的效果；
  本表是用户自己的使用记录，不属于审计域；
- 只追加不修改（问答是一问一答的不可变事实），无删除接口。
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0029_qa_history"
down_revision: str | None = "0028_member_profiles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "qa_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("member_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("project_members.id"), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("sources", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('answered', 'refused')", name="ck_qa_history_status"),
    )
    op.create_index(
        "ix_qa_history_member_time",
        "qa_history",
        ["project_id", "member_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_table("qa_history")
