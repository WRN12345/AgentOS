"""Agent 运行与建议数据模型。

agent_runs 是运行状态、模型、耗时、错误和触发来源的业务事实表，LangGraph 检查点
只用于中断恢复。agent_suggestions 保存结构化建议和人工处理结果；Agent 不得借此
修改正式业务状态。
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.models.base import CoreModel


class AgentRun(CoreModel):
    __tablename__ = "agent_runs"

    # pending → running → succeeded/failed；自动退避重试时回到 pending。
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    agent_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 人工重试必须原样重新投递输入，因此需要持久化 prompt。
    prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trigger_source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    work_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_items.id"), nullable=True, index=True
    )
    # worker 无请求头，必须通过本列传递项目归属。
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # request_id 贯穿 API、队列和 worker，用于排障。
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')", name="ck_agent_runs_status"
        ),
        CheckConstraint(
            "trigger_source IN ('manual', 'scheduler', 'event')",
            name="ck_agent_runs_trigger_source",
        ),
    )


class AgentSuggestion(CoreModel):
    __tablename__ = "agent_suggestions"

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id"), nullable=False, index=True
    )
    suggestion_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    risks: Mapped[str | None] = mapped_column(Text, nullable=True)
    fact_refs: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # accepted/ignored 由人工反馈写入，expired 由周期任务写入。
    review_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        CheckConstraint(
            # 核心记忆提议挂起超过 7 天后进入 expired。
            "review_status IN ('pending', 'accepted', 'ignored', 'expired')",
            name="ck_agent_suggestions_review_status",
        ),
    )
