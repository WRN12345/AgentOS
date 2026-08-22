"""核心记忆提议过期任务（设计文档 16.6，M4.5）。

由 scheduler 周期 enqueue `memory.proposal_expire`、worker 消费执行：
把挂起超 7 天的提议标记 expired（终态，不占待确认列表），防提议无限积压。
失败只记日志（safe_handle_task 兜底），下一轮周期重扫，不产生重试风暴。
"""

from app.core.logging import setup_logging
from app.domains.memory.proposals import expire_stale_proposals
from app.infrastructure.database.engine import async_session_factory

logger = setup_logging("worker")


async def expire_memory_proposals() -> None:
    """扫描并过期挂起超 7 天的核心记忆提议（16.6）。"""
    async with async_session_factory() as session:
        expired = await expire_stale_proposals(session)
    if expired:
        logger.info("memory proposals expired: count=%s", expired)
