"""0030：为文档索引增加可恢复租约。

Revision ID: 0030_file_index_lease
Revises: 0029_qa_history
Create Date: 2026-08-25

index_started_at 仅在索引执行期间非空。worker 定期扫描超时的 indexing
记录并重新投递，以恢复进程中断或 Redis 延迟重投失败后丢失的任务。
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0030_file_index_lease"
down_revision: str | None = "0029_qa_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "stored_files",
        sa.Column("index_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_stored_files_index_recovery",
        "stored_files",
        ["index_status", "index_started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_stored_files_index_recovery", table_name="stored_files")
    op.drop_column("stored_files", "index_started_at")
