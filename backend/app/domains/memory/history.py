"""历史与经验：拆解/分配记录入索引（设计文档第 9 节，M5.1）。

- 只索引"已完成"的拆解/分配运行（15.4）：run 成功且建议已有反馈定论，
  触发时机挂在 submit_suggestion_feedback（采纳/忽略落定后重建索引）；
- 文本内容：输入需求 + 各建议的摘要/理由 + 采纳情况 + 拆解方案要点，
  供检索"上次类似需求是怎么拆的、分给谁、结果如何"；
- 索引为整体重建（rebuild_chunks 语义）：每次反馈后重投任务，
  块内容始终反映最新采纳状态。
"""

import uuid

import redis.asyncio as redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.models import AgentRun, AgentSuggestion
from app.core.logging import setup_logging
from app.domains.memory.indexer import MEMORY_INDEX_TASK_TYPE
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.queue.queue import enqueue

logger = setup_logging("backend")

#: 视为"拆解/分配记录"的 Agent 类型（设计文档第 9 节）
HISTORY_RUN_AGENT_TYPES = ("requirement_pipeline", "requirement_analyst", "assignment_advisor")

_REVIEW_STATUS_LABELS = {
    "accepted": "已采纳",
    "ignored": "已忽略",
    "pending": "待反馈",
    "expired": "已过期",
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


async def enqueue_run_history_index(run: AgentRun) -> None:
    """反馈落定后投递历史索引任务（best-effort：投递失败只记日志，不拖垮主流程）。"""
    redis_client: redis.Redis = create_redis_client()
    try:
        await enqueue(
            redis_client,
            MEMORY_INDEX_TASK_TYPE,
            {
                "project_id": str(run.project_id),
                "source_type": "history",
                "source_id": str(run.id),
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning("history index task enqueue failed: run=%s", run.id, exc_info=True)
    finally:
        await redis_client.aclose()
