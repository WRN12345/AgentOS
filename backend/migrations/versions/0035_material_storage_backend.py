"""Allow verified storage relocation without changing immutable material evidence."""

from alembic import op

revision = "0035_material_storage_backend"
down_revision = "0034_file_directories"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE OR REPLACE FUNCTION protect_project_material() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF NEW.storage_backend IS DISTINCT FROM OLD.storage_backend
                   AND NEW.storage_backend IN ('local', 'minio')
                   AND (to_jsonb(NEW) - 'storage_backend' - 'updated_at')
                       IS NOT DISTINCT FROM (to_jsonb(OLD) - 'storage_backend' - 'updated_at')
                THEN
                    RETURN NEW;
                END IF;
            END IF;
            RAISE EXCEPTION 'Project materials are immutable';
        END; $$
    """)


def downgrade():
    op.execute("""
        CREATE OR REPLACE FUNCTION protect_project_material() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Project materials are immutable';
        END; $$
    """)
