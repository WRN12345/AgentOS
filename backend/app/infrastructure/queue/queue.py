"""基于 Redis 的任务队列：List（LPUSH 入队、BRPOP 出队）+ ZSET 延迟投递。

供 API 进程投递后台任务（如 Agent 运行、到期提醒），Worker 进程消费。
延迟任务（T5.6 Agent 失败指数退避重试）进 ZSET（score=到点时间戳），
worker 每轮循环先把到点任务搬回即时 List 再 BRPOP。
"""

import json
import time
import uuid
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any, cast

import redis.asyncio as redis

QUEUE_KEY = "agentos:tasks"
#: 延迟任务 ZSET：member=任务 JSON，score=到点时间戳（time.time()）
DELAYED_QUEUE_KEY = "agentos:tasks:delayed"


def make_task(task_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "type": task_type,
        "payload": payload or {},
        "enqueued_at": datetime.now(UTC).isoformat(),
    }


async def enqueue(
    client: redis.Redis, task_type: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    task = make_task(task_type, payload)
    await cast(Awaitable[int], client.lpush(QUEUE_KEY, json.dumps(task)))
    return task


async def dequeue(client: redis.Redis, timeout: int = 5) -> dict[str, Any] | None:
    result = await cast(
        Awaitable[list[Any] | None], client.brpop([QUEUE_KEY], timeout=timeout)
    )
    if result is None:
        return None
    _, raw = result
    return json.loads(raw)


async def enqueue_delayed(
    client: redis.Redis,
    task_type: str,
    payload: dict[str, Any] | None = None,
    *,
    delay_seconds: float,
) -> dict[str, Any]:
    """延迟投递：任务进 ZSET，到点后由 worker 的 promote_due_delayed 搬入即时队列。"""
    task = make_task(task_type, payload)
    await cast(
        Awaitable[int],
        client.zadd(DELAYED_QUEUE_KEY, {json.dumps(task): time.time() + delay_seconds}),
    )
    return task


async def promote_due_delayed(client: redis.Redis, *, now: float | None = None) -> int:
    """把到点的延迟任务从 ZSET 搬入即时 List，返回搬移数量（worker 每轮循环调用）。

    BRPOP 阻塞期间到点的任务最多晚一个 dequeue 超时周期被消费，对秒级退避
    间隔无实际影响。单 worker 部署下 ZSET→List 搬移无竞争。
    """
    due = await cast(
        Awaitable[list[Any]],
        client.zrangebyscore(
            DELAYED_QUEUE_KEY, "-inf", now if now is not None else time.time()
        ),
    )
    if not due:
        return 0
    async with client.pipeline() as pipe:
        for raw in due:
            pipe.lpush(QUEUE_KEY, raw)
        pipe.zrem(DELAYED_QUEUE_KEY, *due)
        await pipe.execute()
    return len(due)
