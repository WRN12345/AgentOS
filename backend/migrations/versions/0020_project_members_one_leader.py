"""0020：project_members 每项目仅一名负责人（部分唯一索引）。

Revision ID: 0020_project_members_one_leader
Revises: 0019_audit_project_id
Create Date: 2026-08-17

2026-08-17 规则调整：每项目仅一名负责人，由 admin 指定/变更。
应用层已把角色收敛到 admin（成员接口固定 member 角色，不再暴露 role），
此处再加部分唯一索引作为数据库级保证：同一项目至多一条 role='leader' 的成员记录。

注意：若既有库中已存在同项目多条 leader（历史多负责人数据），迁移会因违反唯一约束失败，
需先人工收敛（保留最早加入的 leader，其余降为 member）。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020_project_members_one_leader"
down_revision: str | None = "0019_audit_project_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ux_project_members_one_leader",
        "project_members",
        ["project_id"],
        unique=True,
        postgresql_where="role = 'leader'",
    )


def downgrade() -> None:
    op.drop_index("ux_project_members_one_leader", table_name="project_members")
