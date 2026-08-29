"""0018：幂等记录纳入项目维度。

idempotency_records 增加 project_id 列；唯一索引由
COALESCE(user_id) + key + method + path 升级为
COALESCE(project_id) + COALESCE(user_id) + key + method + path——
同一用户、同一键、同一路径在 A/B 不同项目下视为不同请求，不复用响应。

存量记录 project_id 为 NULL，落入零值 UUID 桶，保持既有复用行为不变。

Revision ID: 0018_idempotency_project_id
Revises: 0017_agent_runs_project_id
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_idempotency_project_id"
down_revision: str | None = "0017_agent_runs_project_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ZERO_UUID = "00000000-0000-0000-0000-000000000000"


def upgrade() -> None:
    op.add_column(
        "idempotency_records",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.drop_index("ux_idempotency_records_user_key_endpoint", table_name="idempotency_records")
    # 项目、用户、幂等键与端点的组合唯一；项目或用户为空时以零值 UUID 参与唯一性
    op.execute(
        f"""
        CREATE UNIQUE INDEX ux_idempotency_records_project_user_key_endpoint
        ON idempotency_records (
            COALESCE(project_id, '{ZERO_UUID}'::uuid),
            COALESCE(user_id, '{ZERO_UUID}'::uuid),
            key, method, path
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_idempotency_records_project_user_key_endpoint")
    op.execute(
        f"""
        CREATE UNIQUE INDEX ux_idempotency_records_user_key_endpoint
        ON idempotency_records (
            COALESCE(user_id, '{ZERO_UUID}'::uuid),
            key, method, path
        )
        """
    )
    op.drop_column("idempotency_records", "project_id")
