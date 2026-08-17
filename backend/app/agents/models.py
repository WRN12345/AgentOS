"""Agent 运行与建议数据模型（10.2、11 章，T5.2）。

- agent_runs：一次 Agent 图运行的业务记录（状态、模型、耗时、错误、触发来源）。
  这是业务事实表；LangGraph 检查点（checkpoints 等表）只做中断恢复，
  不替代业务记录（原则 1、17.3 节）。
- agent_suggestions：结构化建议 + 人工采纳结果。Agent 只写建议，
  不触碰任何正式业务状态（10.3 节）。
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

    # pending → running → succeeded / failed；自动退避重试期间回 pending（17.3 节，T5.6）
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    # Agent 类型（如 echo / requirement_analyst / workflow_risk …，10.1 节）
    agent_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 人工触发携带的自然语言输入（T5.6：人工重试按原样重新投递，须持久化）
    prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 实际使用的模型名（运行时刻记录，便于追溯）
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 触发来源：manual（人工）/ scheduler（周期）/ event（业务事件）
    trigger_source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    # 关联业务对象（可空：项目级分析不挂工作项）
    work_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_items.id"), nullable=True, index=True
    )
    # 项目归属：所有 Agent 运行均属于项目，worker 无请求头，须靠本列传递。
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 触发方请求 ID（贯穿 API → 队列 → worker 的排障线索）
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
    # 建议类型（10.2 节；如 requirement / assignment / planning / risk / review / summary / echo）
    suggestion_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 结构化建议内容（建议内容和理由，10.2 节）
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 风险和限制（10.2 节）
    risks: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 使用的业务事实引用（如 {"work_item_ids": [...], "member_ids": [...]}）
    fact_refs: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # 人工采纳结果（T5.7 反馈接口写入）
    review_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_members.id"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 提示词版本（10.2 节）
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "review_status IN ('pending', 'accepted', 'ignored')",
            name="ck_agent_suggestions_review_status",
        ),
    )
