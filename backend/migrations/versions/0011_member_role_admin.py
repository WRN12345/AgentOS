"""0011：project_members 角色新增 admin（管理员：查看 + 账号管理）。

Revision ID: 0011_member_role_admin
Revises: 0010_agent_runs_prompt
Create Date: 2026-07-29

本次角色调整：新增第三种角色 admin——可读全部数据并可管理成员账号，
但不参与业务协作（不可被指派为主执行人/协作者/转派目标）。
(a) 放宽 project_members.role 的 CHECK 约束，允许 'admin'；
(b) 既定数据变更：把 username='admin' 初始引导账号的成员角色从
    leader 改为 admin（新部署由 bootstrap 直接创建 admin 成员，
    负责人由管理员登录后通过成员管理创建）。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011_member_role_admin"
down_revision: str | None = "0010_agent_runs_prompt"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_project_members_role", "project_members", type_="check")
    op.create_check_constraint(
        "ck_project_members_role",
        "project_members",
        "role IN ('leader', 'member', 'admin')",
    )
    # 既定数据变更：初始引导账号（username='admin'）转为管理员角色
    op.execute(
        "UPDATE project_members SET role = 'admin' "
        "WHERE role = 'leader' AND user_id IN (SELECT id FROM users WHERE username = 'admin')"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE project_members SET role = 'leader' "
        "WHERE role = 'admin' AND user_id IN (SELECT id FROM users WHERE username = 'admin')"
    )
    op.drop_constraint("ck_project_members_role", "project_members", type_="check")
    op.create_check_constraint(
        "ck_project_members_role",
        "project_members",
        "role IN ('leader', 'member')",
    )
