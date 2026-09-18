"""Isolated project materials and source-grounded requirements."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0032_project_requirements"
down_revision = "0031_memory_chunk_dedup"
branch_labels = None
depends_on = None


def core():
    return [sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("project_id", sa.UUID(), sa.ForeignKey("projects.id"), nullable=False)]


def upgrade():
    op.create_table("project_materials", *core(),
        sa.Column("uploaded_by", sa.UUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False, unique=True),
        sa.Column("storage_backend", sa.String(32), nullable=False),
        sa.Column("chunks", postgresql.JSONB(), nullable=False))
    op.create_table("requirement_analyses", *core(),
        sa.Column("requested_by", sa.UUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("material_ids", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.UUID()),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_delivery_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('pending','running','succeeded','failed')"),
        sa.UniqueConstraint("project_id", "material_ids", name="uq_requirement_analysis_materials"))
    op.create_table("project_requirements", *core(),
        sa.Column("analysis_id", sa.UUID(), sa.ForeignKey("requirement_analyses.id"), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("acceptance_criteria", sa.Text(), nullable=False),
        sa.Column("clarification_questions", sa.Text(), nullable=False),
        sa.Column("sources", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("assignee_id", sa.UUID(), sa.ForeignKey("project_members.id")),
        sa.Column("leader_note", sa.Text()),
        sa.CheckConstraint("status IN ('draft','confirmed','excluded','dispatched','accepted','clarification_requested')"))
    for table in ("project_materials", "requirement_analyses", "project_requirements"):
        op.create_index(f"ix_{table}_project_id", table, ["project_id"])
    op.create_index("ix_requirement_analyses_recovery", "requirement_analyses", ["status", "available_at"])
    op.execute("""
        CREATE FUNCTION protect_project_material() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Project materials are immutable';
        END; $$
    """)
    op.execute("""
        CREATE TRIGGER project_material_immutable BEFORE UPDATE OR DELETE ON project_materials
        FOR EACH ROW EXECUTE FUNCTION protect_project_material()
    """)


def downgrade():
    op.drop_table("project_requirements")
    op.drop_table("requirement_analyses")
    op.drop_table("project_materials")
    op.execute("DROP FUNCTION protect_project_material()")
