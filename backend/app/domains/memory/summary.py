"""从已完成工作项中提炼可复用经验。

工作项审核通过后，后台模型根据结论文本生成简短经验。无可沉淀内容时返回
`NO_EXPERIENCE`，不创建提议。生成结果通过核心记忆审批流交给负责人确认，不能直接生效。
模型错误交由 `worker` 记录，经验总结失败不影响工作项主流程。
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

MEMORY_SUMMARY_TASK_TYPE = "memory.summary"

NO_EXPERIENCE = "无"

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
    """提炼工作项经验；工作项未完成或无可用经验时返回 `None`。

    模型不可用或超时时让异常直接交给 `worker` 处理。
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
    """将经验总结转换为待负责人确认的核心记忆提议。

    提议必须关联 `agent_runs` 以通过外键和运行记录推导项目归属，因此会创建一条
    成功的 `experience_summary` 运行记录。
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
    """工作项审核通过并完成后投递经验总结任务；失败时仅记录日志。"""
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
