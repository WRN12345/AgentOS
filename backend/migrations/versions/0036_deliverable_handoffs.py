"""新增现有工作项之间的交付物交接。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0036_deliverable_handoffs"
down_revision = "0035_material_storage_backend"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "deliverable_handoffs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        sa.Column(
            "source_work_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("work_items.id"),
            nullable=False,
        ),
        sa.Column(
            "target_work_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("work_items.id"),
            nullable=False,
        ),
        sa.Column(
            "deliverable_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("deliverables.id"),
            nullable=False,
        ),
        sa.Column(
            "sender_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_members.id"),
            nullable=False,
        ),
        sa.Column(
            "recipient_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project_members.id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(32), server_default="pending", nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("response_note", sa.Text()),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'changes_requested')",
            name="ck_deliverable_handoffs_status",
        ),
        sa.CheckConstraint(
            "source_work_item_id <> target_work_item_id",
            name="ck_deliverable_handoffs_distinct_items",
        ),
    )
    for column in (
        "project_id",
        "source_work_item_id",
        "target_work_item_id",
        "deliverable_id",
        "sender_id",
        "recipient_id",
    ):
        op.create_index(
            f"ix_deliverable_handoffs_{column}", "deliverable_handoffs", [column]
        )
    op.create_index(
        "uq_deliverable_handoffs_pending_source",
        "deliverable_handoffs",
        ["source_work_item_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade():
    op.drop_table("deliverable_handoffs")
