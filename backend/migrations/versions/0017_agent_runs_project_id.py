"""0017：agent_runs 增加 project_id 冗余列（AI 辅助路径项目化）。

Revision ID: 0017_agent_runs_project_id
Revises: 0016_notifications_files
Create Date: 2026-08-14

给 agent_runs 冗余 project_id（spec D1：有独立列表入口的表冗余 project_id）：
- project_id 可空（兼容历史数据；新写入由服务层保证填写）；
- 历史数据回填：经 work_item_id 推导（work_items.project_id）；
  项目级运行（work_item_id 为空）无推导来源，保持 NULL；
- 加索引，供列表按项目过滤（GET /agent-runs）。
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

revision: str = "0017_agent_runs_project_id"
down_revision: str | None = "0016_notifications_files"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True))
    # 回填：经关联工作项推导（work_items.project_id 是权威）；项目级运行无来源保持 NULL
    op.execute(
        text(
            "UPDATE agent_runs SET project_id = "
            "(SELECT work_items.project_id FROM work_items "
            " WHERE work_items.id = agent_runs.work_item_id) "
            "WHERE project_id IS NULL"
        )
    )
    op.create_foreign_key(
        "fk_agent_runs_project_id", "agent_runs", "projects", ["project_id"], ["id"]
    )
    op.create_index("ix_agent_runs_project_id", "agent_runs", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_runs_project_id", table_name="agent_runs")
    op.drop_constraint("fk_agent_runs_project_id", "agent_runs", type_="foreignkey")
    op.drop_column("agent_runs", "project_id")
