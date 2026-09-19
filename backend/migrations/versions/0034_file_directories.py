"""将文件版本的作用域限定为逻辑目录。"""

from alembic import op
import sqlalchemy as sa

revision = "0034_file_directories"
down_revision = "0033_requirement_discussion"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("stored_files", sa.Column(
        "directory_path", sa.String(512), nullable=False, server_default="/"
    ))
    op.drop_index("ux_stored_files_current_name", table_name="stored_files")
    op.create_index(
        "ux_stored_files_current_name", "stored_files",
        ["project_id", "directory_path", "original_filename"], unique=True,
        postgresql_where=sa.text("superseded_by IS NULL"),
    )


def downgrade():
    # 降级前必须解决跨目录的当前文件名称冲突。
    op.drop_index("ux_stored_files_current_name", table_name="stored_files")
    op.create_index(
        "ux_stored_files_current_name", "stored_files",
        ["project_id", "original_filename"], unique=True,
        postgresql_where=sa.text("superseded_by IS NULL"),
    )
    op.drop_column("stored_files", "directory_path")
