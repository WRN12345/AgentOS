"""0005：协作请求（collaboration_requests）与站内通知（notifications）。

Revision ID: 0005_collaboration_notifications
Revises: 0004_work_items
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_collaboration_notifications"
down_revision: str | None = "0004_work_items"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "collaboration_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), primary_key=True),
        sa.Column("work_item_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("work_items.id"), nullable=False),
        sa.Column("requester_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("project_members.id"), nullable=False),
        sa.Column("assignee_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("project_members.id"), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("template", sa.Text(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_text", sa.Text(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="REQUESTED"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_collaboration_requests_work_item_id", "collaboration_requests", ["work_item_id"])
    op.create_index("ix_collaboration_requests_requester_id", "collaboration_requests", ["requester_id"])
    op.create_index("ix_collaboration_requests_assignee_id", "collaboration_requests", ["assignee_id"])
    op.create_index("ix_collaboration_requests_status", "collaboration_requests", ["status"])

    op.create_table(
        "notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), primary_key=True),
        sa.Column("recipient_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("project_members.id"), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("link", sa.String(512), nullable=True),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_notifications_recipient_id", "notifications", ["recipient_id"])
    op.create_index("ix_notifications_type", "notifications", ["type"])
    op.create_index("ix_notifications_is_read", "notifications", ["is_read"])


def downgrade() -> None:
    op.drop_table("notifications")
    op.drop_table("collaboration_requests")
