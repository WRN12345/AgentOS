"""0013：管理员全局化——users.is_admin + 清理 project_members admin 角色。

Revision ID: 0013_admin_global_and_project_context
Revises: 0012_dev_docs
Create Date: 2026-08-11

admin 从项目内角色升级为全局角色：
(a) users 表新增 is_admin 布尔列，默认 False；
(b) 将现有 admin 成员对应的用户标记为 is_admin=True；
(c) 删除 project_members 中 role='admin' 的记录（admin 不再有成员身份）；
(d) 收紧 project_members.role CHECK 约束，仅允许 'leader'、'member'。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013_admin_global"
down_revision: str | None = "0012_dev_docs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # (a) users 新增 is_admin 列
    op.execute(
        "ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT FALSE"
    )

    # (b) 将现有 admin 成员映射为全局管理员
    op.execute(
        "UPDATE users SET is_admin = TRUE "
        "WHERE id IN (SELECT user_id FROM project_members WHERE role = 'admin')"
    )

    # (c) 删除 admin 成员记录（admin 不再属于任何项目）
    op.execute("DELETE FROM project_members WHERE role = 'admin'")

    # (d) 收紧 CHECK 约束：仅 leader + member
    op.drop_constraint("ck_project_members_role", "project_members", type_="check")
    op.create_check_constraint(
        "ck_project_members_role",
        "project_members",
        "role IN ('leader', 'member')",
    )


def downgrade() -> None:
    # 反向：恢复 admin 角色，但 is_admin 标记的用户无法精确知道原来属于哪个项目
    # ——因此仅恢复约束，不做数据回填（数据已不可逆）。
    # 如需完全回滚，需手动为 is_admin=True 的用户在对应项目中重建 admin 成员记录。
    op.drop_constraint("ck_project_members_role", "project_members", type_="check")
    op.create_check_constraint(
        "ck_project_members_role",
        "project_members",
        "role IN ('leader', 'member', 'admin')",
    )
    op.execute("ALTER TABLE users DROP COLUMN is_admin")
