"""经验总结任务（设计文档第 10 节，M5.3）。

由工作项完成（审核通过）路径 enqueue `memory.summary`、worker 消费执行：

- 每个工作项完成时触发一次轻量总结（16.9）；
- 失败语义与索引任务不同：只记日志、不重试（经验沉淀允许偶尔丢失）；
- 模型不可用静默跳过（ModelError → info 日志），绝不阻塞主流程。

产出接线（总结 → 核心记忆提议）在 M5.4；本任务先只记录总结结果。
"""

import uuid

from app.core.logging import setup_logging
from app.domains.memory.summary import summarize_work_item
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
    # M5.4 接线点：总结产出将在此生成 core memory 提议（走负责人确认通道）
    logger.info("memory summary produced: work_item=%s summary=%s", work_item_id, summary)
