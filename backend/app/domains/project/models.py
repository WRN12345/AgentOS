"""项目、项目成员与成员能力数据模型。

- `projects` 可包含多个项目。
- `project_members` 按项目保存 `leader` 或 `member` 角色、显示名、可投入时间、
  Git 用户名和启用状态；同一用户可属于多个项目。
- `member_capabilities` 保存能力标签与 1 到 5 级熟练度，由成员填报、负责人确认。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.models.base import CoreModel

# 项目角色仅含 `leader` 和 `member`；管理员是 `users.is_admin` 标识的全局角色
ROLE_LEADER = "leader"
ROLE_MEMBER = "member"
ROLES = (ROLE_LEADER, ROLE_MEMBER)


class Project(CoreModel):
    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class ProjectMember(CoreModel):
    __tablename__ = "project_members"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_members_project_user"),
        CheckConstraint("role IN ('leader', 'member')", name="ck_project_members_role"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False, default=ROLE_MEMBER)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    weekly_available_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    git_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    capabilities: Mapped[list["MemberCapability"]] = relationship(
        back_populates="member",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="MemberCapability.tag",
        foreign_keys="MemberCapability.member_id",  # 与 `confirmed_by_member_id` 的外键关系消歧
    )


class MemberCapability(CoreModel):
    __tablename__ = "member_capabilities"
    __table_args__ = (
        UniqueConstraint("member_id", "tag", name="uq_member_capabilities_member_tag"),
        CheckConstraint("proficiency BETWEEN 1 AND 5", name="ck_member_capabilities_proficiency"),
    )

    member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), index=True, nullable=False
    )
    tag: Mapped[str] = mapped_column(String(64), nullable=False)
    proficiency: Mapped[int] = mapped_column(Integer, nullable=False)
    # 成员填报或修改后复位为未确认，只有负责人可以确认
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    confirmed_by_member_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    member: Mapped[ProjectMember] = relationship(
        back_populates="capabilities", foreign_keys=[member_id]
    )
