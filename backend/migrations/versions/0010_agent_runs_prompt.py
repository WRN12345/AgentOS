"""0010：agent_runs 增加 prompt 列，支持人工重新触发运行。

Revision ID: 0010_agent_runs_prompt
Revises: 0009_agent_runs_suggestions
Create Date: 2026-07-28

人工重试失败的 run 时按原输入重新投递 agent.run 任务，因此把触发时携带的
prompt 持久化到 agent_runs（此前只存在于队列任务 payload，消费后即丢失）。
存量行默认空串（等价于当时无 prompt 输入的语义）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_agent_runs_prompt"
down_revision: str | None = "0009_agent_runs_suggestions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("prompt", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "prompt")
