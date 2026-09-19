"""保留多轮需求澄清历史。"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0033_requirement_discussion"
down_revision = "0032_project_requirements"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("project_requirements", sa.Column(
        "discussion", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")))
    # 旧字段仅保留最新问题，不含其原始时间和版本。
    op.execute("""
        UPDATE project_requirements r
        SET discussion = jsonb_build_array(jsonb_build_object(
            'id', gen_random_uuid(),
            'author_id', (SELECT m.user_id FROM project_members m WHERE m.id = r.assignee_id),
            'author_role', 'leader',
            'body', r.leader_note,
            'created_at', NULL,
            'version', NULL
        ))
        WHERE r.leader_note IS NOT NULL AND btrim(r.leader_note) <> ''
    """)


def downgrade():
    op.drop_column("project_requirements", "discussion")
