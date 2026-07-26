"""基线迁移：启用 pgcrypto 扩展（UUID 主键依赖 gen_random_uuid()）。

Revision ID: 0001_baseline
Revises:
Create Date: 2026-07-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS pgcrypto")
