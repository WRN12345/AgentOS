"""基于 Redis List 的任务队列：LPUSH 入队、BRPOP 出队。

供 API 进程投递后台任务（如 Agent 运行、到期提醒），Worker 进程消费。
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as redis

QUEUE_KEY = "agentos:tasks"


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
    await client.lpush(QUEUE_KEY, json.dumps(task))
    return task


async def dequeue(client: redis.Redis, timeout: int = 5) -> dict[str, Any] | None:
    result = await client.brpop(QUEUE_KEY, timeout=timeout)
    if result is None:
        return None
    _, raw = result
    return json.loads(raw)
