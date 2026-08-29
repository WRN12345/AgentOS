"""0019：审计事件项目归属——audit_events 增加 project_id 冗余列。

为需要按项目独立列出的 audit_events 冗余 project_id，
落库时从请求上下文的 X-Project-Id 请求头捕获快照，不在查询时推导：
- project_id 可空：全局动作（登录/登出/管理控制台）无项目上下文，记为 NULL，
  与幂等记录的零值桶语义一致；
- 外键 → projects.id，加索引供列表按项目过滤（负责人只见本项目事件）；
- 存量记录（若有）project_id 为 NULL，表示「项目化改造前的全局事件」。

Revision ID: 0019_audit_project_id
Revises: 0018_idempotency_project_id
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019_audit_project_id"
down_revision: str | None = "0018_idempotency_project_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "audit_events",
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            # 全局动作（登录、登出、管理控制台）允许为空；项目动作必须带项目，
            # HTTP 请求中的 X-Project-Id 既用于校验写操作，也用于记录项目快照。
            comment="项目归属快照（落库时捕获，不靠派生）；NULL=全局动作，项目动作必带项目",
        ),
    )
    op.create_foreign_key(
        "fk_audit_events_project_id", "audit_events", "projects", ["project_id"], ["id"]
    )
    op.create_index("ix_audit_events_project_id", "audit_events", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_project_id", table_name="audit_events")
    op.drop_constraint("fk_audit_events_project_id", "audit_events", type_="foreignkey")
    op.drop_column("audit_events", "project_id")
