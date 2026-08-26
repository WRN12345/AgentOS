"""历史与经验：拆解/分配记录与工作项结论入索引（设计文档第 9 节，M5.1/M5.2）。

- 拆解/分配记录（M5.1）：只索引"已完成"的运行（15.4）——run 成功且仅收录
  已落定（非 pending）的建议，触发时机挂在 submit_suggestion_feedback（反馈落定后重建）；
- 工作项结论（M5.2）：审核通过（approve → COMPLETED）时异步入索引，
  内容含做了什么（标题/描述/验收标准）、验收结果与评审意见（含反馈正文，
  按 2026-08-16 设计文档第 9 节与第 12 节"记忆内容项目内全员可见"，
  2026-08-22 评审确认突破 reviews 反馈的隐私边界）；
- 索引为整体重建（rebuild_chunks 语义），块内容始终反映最新状态。
"""

import uuid

import redis.asyncio as redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.models import AgentRun, AgentSuggestion
from app.core.logging import setup_logging
from app.domains.memory.indexer import MEMORY_INDEX_TASK_TYPE
from app.domains.project.models import ProjectMember
from app.domains.reviews.models import Review
from app.domains.work_items.models import WorkItem
from app.domains.work_items.state_machine import WorkItemStatus
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.queue.queue import enqueue

logger = setup_logging("backend")

#: 视为"拆解/分配记录"的 Agent 类型（设计文档第 9 节）
HISTORY_RUN_AGENT_TYPES = ("requirement_pipeline", "requirement_analyst", "assignment_advisor")

#: 历史来源种类（worker 据此取文本）：拆解/分配运行 / 工作项结论
HISTORY_KIND_RUN = "run"
HISTORY_KIND_WORK_ITEM = "work_item"

_REVIEW_STATUS_LABELS = {
    "accepted": "已采纳",
    "ignored": "已忽略",
    "pending": "待反馈",
    "expired": "已过期",
}

_DECISION_LABELS = {
    "approve": "通过",
    "request_changes": "要求修改",
    "reject": "拒绝",
}


async def build_run_history_text(
    session: AsyncSession, run_id: uuid.UUID
) -> str | None:
    """组装一次拆解/分配运行的可检索文本；不满足"已完成"口径返回 None（不索引）。"""
    run = await session.get(AgentRun, run_id)
    if run is None or run.agent_type not in HISTORY_RUN_AGENT_TYPES:
        return None
    if run.status != "succeeded":
        return None  # 进行中的不索引（15.4）

    suggestions = (
        await session.execute(
            select(AgentSuggestion)
            .where(AgentSuggestion.run_id == run.id)
            .order_by(AgentSuggestion.created_at)
        )
    ).scalars().all()

    parts = [f"需求拆解/分配记录\n\n输入需求：\n{run.prompt}"]
    for s in suggestions:
        # 一次运行可产出多条建议。首次反馈时其余建议仍可能待确认，不能让
        # 未经人工定论的内容进入可检索历史；后续反馈会整体重建该来源。
        if s.review_status == "pending":
            continue
        label = _REVIEW_STATUS_LABELS.get(s.review_status, s.review_status)
        section = f"\n\n## 建议（{s.suggestion_type}）—— {label}"
        summary = s.content.get("summary")
        if summary:
            section += f"\n摘要：{summary}"
        rationale = s.content.get("rationale")
        if rationale:
            section += f"\n理由：{rationale}"
        breakdown = s.content.get("work_item_breakdown")
        if isinstance(breakdown, list) and breakdown:
            lines = []
            for item in breakdown:
                if not isinstance(item, dict):
                    continue
                title = item.get("title", "")
                assignee = item.get("recommended_assignee") or {}
                assignee_name = (
                    assignee.get("display_name") if isinstance(assignee, dict) else None
                )
                lines.append(
                    f"- {title}" + (f"（推荐：{assignee_name}）" if assignee_name else "")
                )
            section += "\n拆解方案：\n" + "\n".join(lines)
        parts.append(section)
    return "".join(parts)


async def _enqueue_history_index(
    project_id: uuid.UUID, source_id: uuid.UUID, kind: str
) -> None:
    """投递历史索引任务（best-effort：投递失败只记日志，不拖垮主流程）。"""
    redis_client: redis.Redis = create_redis_client()
    try:
        await enqueue(
            redis_client,
            MEMORY_INDEX_TASK_TYPE,
            {
                "project_id": str(project_id),
                "source_type": "history",
                "source_id": str(source_id),
                "history_kind": kind,
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "history index task enqueue failed: kind=%s id=%s", kind, source_id, exc_info=True
        )
    finally:
        await redis_client.aclose()


async def enqueue_run_history_index(run: AgentRun) -> None:
    """反馈落定后投递拆解/分配记录索引任务（M5.1）。"""
    await _enqueue_history_index(run.project_id, run.id, HISTORY_KIND_RUN)


async def build_work_item_conclusion_text(
    session: AsyncSession, work_item_id: uuid.UUID
) -> str | None:
    """组装已完成工作项的结论文本（M5.2）；未完成或不存在返回 None（不索引）。"""
    item = await session.get(WorkItem, work_item_id)
    if item is None or item.status != WorkItemStatus.COMPLETED.value:
        return None  # 只索引已完成（15.4）

    assignee = await session.get(ProjectMember, item.assignee_id)
    parts = [
        f"工作项完成记录\n\n标题：{item.title}",
        f"\n主执行人：{assignee.display_name if assignee else '未知'}",
    ]
    if item.description:
        parts.append(f"\n\n做了什么：\n{item.description}")
    if item.acceptance_criteria:
        parts.append(f"\n\n验收标准：\n{item.acceptance_criteria}")

    reviews = (
        await session.execute(
            select(Review)
            .where(Review.work_item_id == item.id)
            .order_by(Review.created_at)
        )
    ).scalars().all()
    if reviews:
        parts.append("\n\n验收与评审：")
        for r in reviews:
            label = _DECISION_LABELS.get(r.decision, r.decision)
            section = f"\n- 结论：{label}"
            if r.feedback:
                section += f"；评审意见：{r.feedback}"
            parts.append(section)
    return "".join(parts)


async def enqueue_work_item_conclusion_index(item: WorkItem) -> None:
    """工作项完成（审核通过）后投递结论索引任务（M5.2）。"""
    await _enqueue_history_index(item.project_id, item.id, HISTORY_KIND_WORK_ITEM)
