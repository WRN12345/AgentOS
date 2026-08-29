"""0007：创建 stored_files 文件存储记录表。

Revision ID: 0007_stored_files
Revises: 0006_transfers_deadline_changes
Create Date: 2026-07-27

数据库仅保存相对 storage_key 与存储后端名，不保存宿主机绝对路径；
work_item_id 可空，用于文件按需关联工作项，未关联文件仍可独立存储。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_stored_files"
down_revision: str | None = "0006_transfers_deadline_changes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stored_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), primary_key=True),
        sa.Column("storage_backend", sa.String(32), nullable=False, server_default="local"),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("uploaded_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("project_members.id"), nullable=False),
        # 文件可按需关联工作项，未关联文件保持为空
        sa.Column("work_item_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("work_items.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_stored_files_uploaded_by", "stored_files", ["uploaded_by"])
    op.create_index("ix_stored_files_work_item_id", "stored_files", ["work_item_id"])
    op.create_index("uq_stored_files_storage_key", "stored_files", ["storage_key"], unique=True)


def downgrade() -> None:
    op.drop_table("stored_files")
