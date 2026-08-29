"""0015：交付物项目化——deliverables 增加 project_id 冗余列。

Revision ID: 0015_deliverables_project_id
Revises: 0014_work_items_project_id
Create Date: 2026-08-11

为需要按项目独立列出的 deliverables 冗余 project_id：
- project_id NOT NULL，外键 → projects.id；
- 历史数据经所属工作项推导（work_items.project_id，
  deliverables 归属 = 其工作项的项目归属）；
- 加索引，供列表按项目过滤。
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

revision: str = "0015_deliverables_project_id"
down_revision: str | None = "0014_work_items_project_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 先加可空列，回填后再收紧为 NOT NULL（否则已有行无法加约束）
    op.add_column("deliverables", sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True))
    # 回填：交付物归属 = 其所属工作项的项目归属（work_items.project_id 已是权威）
    op.execute(
        text(
            "UPDATE deliverables SET project_id = "
            "(SELECT work_items.project_id FROM work_items "
            " WHERE work_items.id = deliverables.work_item_id) "
            "WHERE project_id IS NULL"
        )
    )
    op.alter_column("deliverables", "project_id", nullable=False)
    op.create_foreign_key(
        "fk_deliverables_project_id", "deliverables", "projects", ["project_id"], ["id"]
    )
    op.create_index("ix_deliverables_project_id", "deliverables", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_deliverables_project_id", table_name="deliverables")
    op.drop_constraint("fk_deliverables_project_id", "deliverables", type_="foreignkey")
    op.drop_column("deliverables", "project_id")
