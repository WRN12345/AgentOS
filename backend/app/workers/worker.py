"""后台 Worker 入口（4.2 节）。

Worker 与 API 共用 domains/ 与 infrastructure/ 的同一套领域模型和数据访问层，
从 Redis 队列消费后台任务（Agent 运行、到期提醒等）。

约束（4.2 节）：Worker 不得调用"批准、转派、完成"等业务命令，
只允许生成建议或通知。本骨架中不引入任何业务命令代码路径。
"""

import asyncio

from app.core.logging import setup_logging
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.queue.queue import dequeue
from app.workers.heartbeat import heartbeat

logger = setup_logging("worker")


async def handle_task(task: dict) -> None:
    task_type = task.get("type", "<unknown>")
    if task_type == "example.ping":
        logger.info(
            "consumed example task: id=%s source=%s",
            task.get("id"),
            task.get("payload", {}).get("source", "<unknown>"),
        )
    else:
        logger.warning("unknown task type, skipped: id=%s type=%s", task.get("id"), task_type)


async def run() -> None:
    logger.info("worker started, waiting for tasks")
    redis_client = create_redis_client()
    try:
        while True:
            await heartbeat(redis_client, "worker")
            task = await dequeue(redis_client, timeout=5)
            if task is not None:
                await handle_task(task)
    finally:
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(run())
