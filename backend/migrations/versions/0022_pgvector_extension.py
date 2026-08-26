"""0022：启用 pgvector 扩展——记忆模块语义检索的向量存储基础。

Revision ID: 0022_pgvector_extension
Revises: 0021_agent_runs_project_not_null
Create Date: 2026-08-21
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0022_pgvector_extension"
down_revision: str | None = "0021_agent_runs_project_not_null"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))


def downgrade() -> None:
    op.execute(text("DROP EXTENSION IF EXISTS vector"))
