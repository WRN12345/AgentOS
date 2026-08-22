"""经验总结任务（设计文档第 10 节，M5.3）。

由工作项完成（审核通过）路径 enqueue `memory.summary`、worker 消费执行：

- 每个工作项完成时触发一次轻量总结（16.9）；
- 失败语义与索引任务不同：只记日志、不重试（经验沉淀允许偶尔丢失）；
- 模型不可用静默跳过（ModelError → info 日志），绝不阻塞主流程；
- 产出接线（M5.4）：总结文字自动生成核心记忆提议（memory_proposal），
  走 M4.4 负责人确认通道，不直接生效。
"""

import uuid

from app.core.logging import setup_logging
from app.domains.memory.summary import create_experience_proposal, summarize_work_item
from app.domains.work_items.models import WorkItem
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.models.errors import ModelError

logger = setup_logging("worker.memory_summary")


async def execute_memory_summary(payload: dict) -> None:
    """执行一次经验总结：模型不可用静默跳过，其余异常记日志后放弃（不重试）。"""
    work_item_id = payload.get("work_item_id")
    if not work_item_id:
        logger.warning("memory summary task missing work_item_id, skipped")
        return
    try:
        async with async_session_factory() as session:
            summary = await summarize_work_item(uuid.UUID(str(work_item_id)), session)
    except ModelError as exc:
        # 模型不可用静默跳过（16.9）：不留重试，不留失败状态
        logger.info(
            "memory summary skipped (model unavailable): work_item=%s %s",
            work_item_id,
            type(exc).__name__,
        )
        return
    except Exception:  # noqa: BLE001
        logger.error(
            "memory summary failed, dropped (no retry): work_item=%s",
            work_item_id,
            exc_info=True,
        )
        return

    if summary is None:
        logger.info("memory summary: no reusable experience, work_item=%s", work_item_id)
        return
    # M5.4：总结产出自动生成核心记忆提议，走负责人确认通道（不直接生效）
    async with async_session_factory() as session:
        item = await session.get(WorkItem, uuid.UUID(str(work_item_id)))
        if item is None:
            logger.warning("memory summary: work item gone, skipped: %s", work_item_id)
            return
        proposal = await create_experience_proposal(session, item=item, summary=summary)
    logger.info(
        "memory summary proposed: work_item=%s suggestion=%s", work_item_id, proposal.id
    )
