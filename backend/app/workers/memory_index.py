"""记忆索引任务（M1.8，设计文档第 6、13 节）。

任务类型 `memory.index`，payload：
- `project_id`：str | None（profile 类型为 None，随人走，16.12）；
- `source_type` / `source_id` / `text`：来源类型、来源实体 ID、待索引纯文本
  （M2.5 内容提取器就位前由调用方直接携带文本）；
- `attempt`：已重试次数（重入队时自增）。

重试策略（与总结任务不同，索引任务允许重试）：任何失败（embedding 不可用、
DB 瞬断等）按指数退避进延迟队列，最多 MAX_ATTEMPTS 次；耗尽后只记错误日志，
文档侧的"索引失败"状态标注由 M2.4 状态机负责。
"""

import uuid

import redis.asyncio as redis

from app.core.logging import setup_logging
from app.domains.memory.indexer import MemoryIndexService
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.queue.queue import enqueue_delayed

logger = setup_logging("worker.memory_index")

TASK_TYPE = "memory.index"
#: 最大重试次数（不含首次执行）与指数退避基数（秒）：10s → 20s → 40s
MAX_ATTEMPTS = 3
RETRY_BASE_SECONDS = 10.0


async def execute_memory_index(payload: dict, redis_client: redis.Redis) -> None:
    source_type = payload.get("source_type", "<unknown>")
    source_id = payload.get("source_id", "<unknown>")
    attempt = int(payload.get("attempt", 0))
    try:
        project_id = uuid.UUID(payload["project_id"]) if payload.get("project_id") else None
        async with async_session_factory() as session:
            service = MemoryIndexService(session)
            written = await service.rebuild_chunks(
                project_id=project_id,
                source_type=str(source_type),
                source_id=uuid.UUID(str(source_id)),
                text=str(payload.get("text", "")),
            )
        logger.info(
            "memory index done: type=%s id=%s chunks=%d", source_type, source_id, written
        )
    except Exception:
        if attempt < MAX_ATTEMPTS:
            delay = RETRY_BASE_SECONDS * (2**attempt)
            await enqueue_delayed(
                redis_client,
                TASK_TYPE,
                {**payload, "attempt": attempt + 1},
                delay_seconds=delay,
            )
            logger.warning(
                "memory index failed, retry %d/%d in %.0fs: type=%s id=%s",
                attempt + 1,
                MAX_ATTEMPTS,
                delay,
                source_type,
                source_id,
                exc_info=True,
            )
        else:
            logger.error(
                "memory index failed after %d retries, dropped: type=%s id=%s",
                MAX_ATTEMPTS,
                source_type,
                source_id,
                exc_info=True,
            )
