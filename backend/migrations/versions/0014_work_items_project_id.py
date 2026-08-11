"""0014：工作项项目化——work_items 增加 project_id 冗余列。

Revision ID: 0014_work_items_project_id
Revises: 0013_admin_global
Create Date: 2026-08-11

给「有独立列表入口」的 work_items 冗余 project_id（spec D1/D3）：
- project_id NOT NULL，外键 → projects.id；
- 历史数据回填：无存量兼容义务（D5 重建），全部归属当前唯一项目
  （get_default_project 语义：按 created_at 最早的 project 回填）；
- 加索引，供列表按项目过滤。
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

revision: str = "0014_work_items_project_id"
down_revision: str | None = "0013_admin_global"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 先加可空列，回填后再收紧为 NOT NULL（否则已有行无法加约束）
    op.add_column("work_items", sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True))
    # 回填当前唯一项目：按 created_at 最早的 project（与 get_default_project 语义一致）
    op.execute(
        text(
            "UPDATE work_items SET project_id = "
            "(SELECT id FROM projects ORDER BY created_at LIMIT 1) "
            "WHERE project_id IS NULL"
        )
    )
    op.alter_column("work_items", "project_id", nullable=False)
    op.create_foreign_key(
        "fk_work_items_project_id", "work_items", "projects", ["project_id"], ["id"]
    )
    op.create_index("ix_work_items_project_id", "work_items", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_work_items_project_id", table_name="work_items")
    op.drop_constraint("fk_work_items_project_id", "work_items", type_="foreignkey")
    op.drop_column("work_items", "project_id")
