"""将已落定的拆解、分配记录和工作项结论写入记忆索引。

仅成功运行且已落定的建议进入拆解/分配历史，待确认建议不会被索引。工作项只有在
审核通过并完成后才异步索引，内容包含描述、验收标准、验收结果和评审意见，因此会在
项目内记忆检索中可见。索引按来源整体重建，始终反映最新状态。
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

#: 会沉淀为拆解或分配历史的 `Agent` 类型
HISTORY_RUN_AGENT_TYPES = ("requirement_pipeline", "requirement_analyst", "assignment_advisor")

#: `worker` 根据来源种类选择运行记录或工作项结论
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
    """组装拆解或分配运行的可检索文本；未成功完成时返回 `None`。"""
    run = await session.get(AgentRun, run_id)
    if run is None or run.agent_type not in HISTORY_RUN_AGENT_TYPES:
        return None
    if run.status != "succeeded":
        return None

    suggestions = (
        await session.execute(
            select(AgentSuggestion)
            .where(AgentSuggestion.run_id == run.id)
            .order_by(AgentSuggestion.created_at)
        )
    ).scalars().all()

    parts = [f"需求拆解/分配记录\n\n输入需求：\n{run.prompt}"]
    for s in suggestions:
        # 只让人工已定论的建议进入检索，后续反馈会整体重建该来源
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
    """投递历史索引任务；失败时仅记录日志，不阻塞主流程。"""
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
    """反馈落定后投递拆解或分配记录的索引任务。"""
    await _enqueue_history_index(run.project_id, run.id, HISTORY_KIND_RUN)


async def build_work_item_conclusion_text(
    session: AsyncSession, work_item_id: uuid.UUID
) -> str | None:
    """组装已完成工作项的结论文本；未完成或不存在时返回 `None`。"""
    item = await session.get(WorkItem, work_item_id)
    if item is None or item.status != WorkItemStatus.COMPLETED.value:
        return None

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
    """工作项审核通过并完成后投递结论索引任务。"""
    await _enqueue_history_index(item.project_id, item.id, HISTORY_KIND_WORK_ITEM)
