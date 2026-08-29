"""核心记忆提议过期任务。

由 scheduler 周期 enqueue `memory.proposal_expire`、worker 消费执行：
把挂起超 7 天的提议标记 expired（终态，不占待确认列表），防提议无限积压。
失败由 `safe_handle_task` 记录，等待下一轮周期扫描，避免产生重试风暴。
"""

from app.core.logging import setup_logging
from app.domains.memory.proposals import expire_stale_proposals
from app.infrastructure.database.engine import async_session_factory

logger = setup_logging("worker")


async def expire_memory_proposals() -> None:
    """扫描并过期挂起超过 7 天的核心记忆提议。"""
    async with async_session_factory() as session:
        expired = await expire_stale_proposals(session)
    if expired:
        logger.info("memory proposals expired: count=%s", expired)
