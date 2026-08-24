"""0028：member_profiles 表——成员文字档案（设计文档第 7 节②，M3.4）。

Revision ID: 0028_member_profiles
Revises: 0027_suggestion_review_expired
Create Date: 2026-08-24

- 随人走、不挂项目（跨项目可见的唯一例外，16.12）：键为 users.id——
  project_members 是每项目一条的成员身份，跨项目的"人"只有 users；
- 一人一份档案（user_id 唯一）；记录创建/最近编辑者（负责人，15.6）；
- 不设停用列：成员停用以 users.is_active 为准（16.7 档案保留、不进分配候选，
  由查询侧 join 判定，避免冗余标记与账号状态不一致）。
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0028_member_profiles"
down_revision: str | None = "0027_suggestion_review_expired"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "member_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_by_member_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("project_members.id"), nullable=False),
        sa.Column("last_edited_by_member_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("project_members.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", name="uq_member_profiles_user"),
    )


def downgrade() -> None:
    op.drop_table("member_profiles")
