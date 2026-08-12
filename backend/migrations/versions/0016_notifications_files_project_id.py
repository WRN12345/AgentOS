"""0016：通知与文件项目化——notifications、stored_files 增加 project_id 冗余列。

Revision ID: 0016_notifications_files
Revises: 0015_deliverables_project_id
Create Date: 2026-08-12

给「有独立列表入口」的 notifications、stored_files 冗余 project_id（spec D1/D3）：
- project_id NOT NULL，外键 → projects.id；
- 历史数据回填（无存量兼容义务，D5 重建，形式性回填）：
  notifications 经 recipient 的成员记录推导（recipient → project_members.project_id）；
  stored_files 优先经 work_item_id 推导（work_items.project_id），
  未关联工作项的经 uploaded_by 的成员记录推导；
- 加索引，供列表按项目过滤。
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

revision: str = "0016_notifications_files"
down_revision: str | None = "0015_deliverables_project_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- notifications ---
    op.add_column("notifications", sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True))
    # 回填：通知归属 = 接收人成员记录的项目归属（project_members.project_id 是权威）
    op.execute(
        text(
            "UPDATE notifications SET project_id = "
            "(SELECT project_members.project_id FROM project_members "
            " WHERE project_members.id = notifications.recipient_id) "
            "WHERE project_id IS NULL"
        )
    )
    op.alter_column("notifications", "project_id", nullable=False)
    op.create_foreign_key(
        "fk_notifications_project_id", "notifications", "projects", ["project_id"], ["id"]
    )
    op.create_index("ix_notifications_project_id", "notifications", ["project_id"])

    # --- stored_files ---
    op.add_column("stored_files", sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True))
    # 回填：优先经关联工作项推导，未关联的经上传人成员记录推导
    op.execute(
        text(
            "UPDATE stored_files SET project_id = COALESCE("
            "(SELECT work_items.project_id FROM work_items "
            " WHERE work_items.id = stored_files.work_item_id), "
            "(SELECT project_members.project_id FROM project_members "
            " WHERE project_members.id = stored_files.uploaded_by)) "
            "WHERE project_id IS NULL"
        )
    )
    op.alter_column("stored_files", "project_id", nullable=False)
    op.create_foreign_key(
        "fk_stored_files_project_id", "stored_files", "projects", ["project_id"], ["id"]
    )
    op.create_index("ix_stored_files_project_id", "stored_files", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_stored_files_project_id", table_name="stored_files")
    op.drop_constraint("fk_stored_files_project_id", "stored_files", type_="foreignkey")
    op.drop_column("stored_files", "project_id")

    op.drop_index("ix_notifications_project_id", table_name="notifications")
    op.drop_constraint("fk_notifications_project_id", "notifications", type_="foreignkey")
    op.drop_column("notifications", "project_id")
