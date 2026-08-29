"""0024：为 stored_files 增加版本链，实现知识文档版本化。

Revision ID: 0024_file_versions
Revises: 0023_memory_chunks
Create Date: 2026-08-21

- version：同项目内同名文件的版本号，既有数据一律视为 v1；
- superseded_by：旧版本指向取代它的新版本；NULL 即"当前最新版本"；
- 部分唯一索引 ux_stored_files_current_name：同项目同文件名至多一个当前版本，
  数据库级防止并发同名上传产生两个"最新版"；
- 文档不可删除：旧版本永久保留供人工追溯，检索只命中当前版本。

注意 superseded_by 的自引用外键必须 DEFERRABLE INITIALLY DEFERRED：
版本更替事务里要"先把旧版本指向新版本、再插入新版本行"（部分唯一索引的要求），
外键推迟到 COMMIT 时检查才能打破"互相等待对方先存在"的死锁。
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0024_file_versions"
down_revision: str | None = "0023_memory_chunks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "stored_files",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "stored_files",
        sa.Column(
            "superseded_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("stored_files.id", deferrable=True, initially="DEFERRED"),
            nullable=True,
        ),
    )
    op.create_index(
        "ux_stored_files_current_name",
        "stored_files",
        ["project_id", "original_filename"],
        unique=True,
        postgresql_where="superseded_by IS NULL",
    )


def downgrade() -> None:
    op.drop_index("ux_stored_files_current_name", table_name="stored_files")
    op.drop_column("stored_files", "superseded_by")
    op.drop_column("stored_files", "version")
