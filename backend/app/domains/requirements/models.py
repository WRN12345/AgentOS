import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.models.base import CoreModel, VersionMixin


class Material(CoreModel):
    __tablename__ = "project_materials"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    original_filename: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str] = mapped_column(Text, unique=True)
    storage_backend: Mapped[str] = mapped_column(String(32))
    chunks: Mapped[list[dict]] = mapped_column(JSONB)


class Analysis(CoreModel):
    __tablename__ = "requirement_analyses"
    __table_args__ = (
        CheckConstraint("status IN ('pending','running','succeeded','failed')"),
        UniqueConstraint("project_id", "material_ids", name="uq_requirement_analysis_materials"),
        Index("ix_requirement_analyses_recovery", "status", "available_at"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    material_ids: Mapped[list[str]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[uuid.UUID | None]
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_delivery_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Requirement(CoreModel, VersionMixin):
    __tablename__ = "project_requirements"
    __table_args__ = (CheckConstraint(
        "status IN ('draft','confirmed','excluded','dispatched','accepted','clarification_requested')"
    ),)

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    analysis_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("requirement_analyses.id"))
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text)
    acceptance_criteria: Mapped[str] = mapped_column(Text)
    clarification_questions: Mapped[str] = mapped_column(Text)
    sources: Mapped[list[dict]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("project_members.id"))
    leader_note: Mapped[str | None] = mapped_column(Text)
    discussion: Mapped[list[dict]] = mapped_column(JSONB, default=list, server_default="[]")
