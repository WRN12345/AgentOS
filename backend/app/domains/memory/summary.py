"""闭环经验总结（设计文档第 10 节，M5.3）。

工作项完成（审核通过）后，后台让模型回看这次过程，提炼可复用的经验文字。

- 输入：工作项结论文本（复用 M5.2 的 build_work_item_conclusion_text）；
- 输出：一两句可复用经验；模型判断"没有值得沉淀的"时返回约定标记 NO_EXPERIENCE，
  调用方据此跳过（不产出提议）；
- 产出接线（M5.4）：create_experience_proposal 把经验文字生成 memory_proposal，
  走负责人确认通道——经验不直接生效（第 10 节）；
- 模型错误（ModelError）不由本模块兜底，交给 worker 任务层按 16.9 处理：
  只记日志、不重试——经验沉淀允许偶尔丢失。
"""

import uuid

import redis.asyncio as redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import setup_logging
from app.agents.models import AgentRun, AgentSuggestion
from app.domains.memory.history import build_work_item_conclusion_text
from app.domains.memory.proposals import create_memory_proposal
from app.domains.work_items.models import WorkItem
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.models.provider import get_model_provider
from app.infrastructure.queue.queue import enqueue

logger = setup_logging("backend")

#: 经验总结任务类型（reviews 完成路径投递、worker 消费共用）
MEMORY_SUMMARY_TASK_TYPE = "memory.summary"

#: 模型表示"无可沉淀经验"的约定输出
NO_EXPERIENCE = "无"

#: 经验总结运行记录的 agent_type（提议须经 run 推导项目归属，M5.4）
EXPERIENCE_SUMMARY_AGENT_TYPE = "experience_summary"

_SUMMARY_SYSTEM = (
    "你是项目经验总结助手。回看一次已完成工作项的过程记录，"
    "提炼一两条对今后拆解、分配或执行类似任务有可复用价值的经验"
    "（例如：这类需求上次拆成了几个工作项、哪类模块改动总要同步动哪里、"
    "哪类任务适合交给什么背景的人）。"
    "只输出经验本身，每条一句话，不要复述过程，不要客套。"
    "如果这次过程没有值得沉淀的经验，只输出：无"
)


async def summarize_work_item(
    work_item_id: uuid.UUID, session: AsyncSession
) -> str | None:
    """回看工作项过程并提炼经验，返回经验文字；未完成/无经验返回 None。

    ModelError（不可用/超时）直接冒泡给调用方（worker 按 16.9 只记日志）。
    """
    conclusion = await build_work_item_conclusion_text(session, work_item_id)
    if conclusion is None:
        return None

    provider = get_model_provider()
    text = (
        await provider.generate(
            f"以下是一次已完成工作项的过程记录：\n\n{conclusion}",
            system=_SUMMARY_SYSTEM,
        )
    ).strip()
    if not text or text == NO_EXPERIENCE:
        return None
    return text


async def create_experience_proposal(
    session: AsyncSession, *, item: WorkItem, summary: str
) -> AgentSuggestion:
    """总结产出自动生成核心记忆提议（M5.4，第 10 节）：走 M4.4 确认通道，不直接生效。

    提议须挂在 agent_runs 上（FK + 项目归属推导）：补一条 experience_summary
    运行记录（trigger_source=event，status=succeeded——总结本身即本次运行）。
    """
    run = AgentRun(
        status="succeeded",
        agent_type=EXPERIENCE_SUMMARY_AGENT_TYPE,
        trigger_source="event",
        work_item_id=item.id,
        project_id=item.project_id,
        model=settings.llm_model or None,
    )
    session.add(run)
    await session.flush()
    return await create_memory_proposal(
        session,
        run=run,
        action="create",
        content=summary,
        reason=f"工作项「{item.title}」完成后自动沉淀的经验",
    )


async def enqueue_work_item_summary(item: WorkItem) -> None:
    """工作项完成（审核通过）后投递经验总结任务（M5.3，best-effort）。"""
    redis_client: redis.Redis = create_redis_client()
    try:
        await enqueue(
            redis_client,
            MEMORY_SUMMARY_TASK_TYPE,
            {
                "project_id": str(item.project_id),
                "work_item_id": str(item.id),
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "memory summary task enqueue failed: work_item=%s", item.id, exc_info=True
        )
    finally:
        await redis_client.aclose()
