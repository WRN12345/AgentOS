"""0025：stored_files 增加索引状态（设计文档第 6 节）。

Revision ID: 0025_file_index_status
Revises: 0024_file_versions
Create Date: 2026-08-21

状态机：pending（待处理）→ indexing（索引中）→ indexed（已索引）
                          ↘ failed（失败，可重试回 pending）
        不支持读取内容的格式（zip/图片等）→ unindexed（未索引，终态）

既有数据不回填索引（本期只对"上传即入库"的新文件建索引），
历史文件一律标记 unindexed；如需检索可重新上传生成新版本触发索引。
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0025_file_index_status"
down_revision: str | None = "0024_file_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "stored_files",
        sa.Column("index_status", sa.String(16), nullable=False, server_default="pending"),
    )
    op.create_check_constraint(
        "ck_stored_files_index_status",
        "stored_files",
        "index_status IN ('pending', 'indexing', 'indexed', 'failed', 'unindexed')",
    )
    # 既有文件不回填索引，标记 unindexed（注释见模块 docstring）
    op.execute("UPDATE stored_files SET index_status = 'unindexed'")


def downgrade() -> None:
    op.drop_constraint("ck_stored_files_index_status", "stored_files", type_="check")
    op.drop_column("stored_files", "index_status")
