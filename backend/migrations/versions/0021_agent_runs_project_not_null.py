"""0021：Agent 运行项目归属改为必填。

Revision ID: 0021_agent_runs_project_not_null
Revises: 0020_project_members_one_leader
Create Date: 2026-08-17
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision: str = "0021_agent_runs_project_not_null"
down_revision: str | None = "0020_project_members_one_leader"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        text(
            "UPDATE agent_runs AS ar SET project_id = wi.project_id "
            "FROM work_items AS wi "
            "WHERE ar.project_id IS NULL AND ar.work_item_id = wi.id"
        )
    )
    op.execute(
        text(
            "DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM agent_runs WHERE project_id IS NULL) THEN "
            "RAISE EXCEPTION 'agent_runs contains project-less legacy rows; map them before migration'; "
            "END IF; END $$"
        )
    )
    op.alter_column("agent_runs", "project_id", existing_type=sa.Uuid(), nullable=False)


def downgrade() -> None:
    op.alter_column("agent_runs", "project_id", existing_type=sa.Uuid(), nullable=True)
