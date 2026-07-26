"""Scheduler 入口（4.2 节）。

按配置周期触发后台任务。首版只注册并触发一个示例任务；
阶段 3/5 的到期提醒、逾期风险扫描、日报等调度在此挂接。
"""

import asyncio

from app.core.config import settings
from app.core.logging import setup_logging
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.queue.queue import enqueue
from app.workers.heartbeat import heartbeat

logger = setup_logging("scheduler")


async def run() -> None:
    interval = settings.scheduler_example_interval_seconds
    logger.info("scheduler started, example task interval=%ss", interval)
    redis_client = create_redis_client()
    try:
        while True:
            await heartbeat(redis_client, "scheduler")
            task = await enqueue(redis_client, "example.ping", {"source": "scheduler"})
            logger.info("scheduler triggered example task: id=%s", task["id"])
            await asyncio.sleep(interval)
    finally:
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(run())
