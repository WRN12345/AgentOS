"""0027：agent_suggestions.review_status 增加 expired 终态。

Revision ID: 0027_suggestion_review_expired
Revises: 0026_core_memory_entries
Create Date: 2026-08-22

核心记忆提议挂起超 7 天自动过期：review_status 新增终态 expired，
过期提议不占待确认列表，Agent 认为仍重要可重新提议。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0027_suggestion_review_expired"
down_revision: str | None = "0026_core_memory_entries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_agent_suggestions_review_status", "agent_suggestions", type_="check"
    )
    op.create_check_constraint(
        "ck_agent_suggestions_review_status",
        "agent_suggestions",
        "review_status IN ('pending', 'accepted', 'ignored', 'expired')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_agent_suggestions_review_status", "agent_suggestions", type_="check"
    )
    op.create_check_constraint(
        "ck_agent_suggestions_review_status",
        "agent_suggestions",
        "review_status IN ('pending', 'accepted', 'ignored')",
    )
