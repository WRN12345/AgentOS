"""Scheduler 入口（4.2 节）。

按配置周期触发后台任务，实际执行均由 Worker 消费：

- `example.ping`：工程链路示例任务（SCHEDULER_EXAMPLE_INTERVAL_SECONDS）；
- `due.scan`：到期/逾期提醒扫描（DUE_SCAN_INTERVAL_SECONDS，默认 300s，T3.6）。

单循环按各自周期触发：记录上次触发时间，到达周期即 enqueue，
睡眠取各周期的最小值。阶段 5 的逾期风险扫描、日报等调度在此同样挂接。
"""

import asyncio
import time

from app.core.config import settings
from app.core.logging import setup_logging
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.queue.queue import enqueue
from app.workers.heartbeat import heartbeat

logger = setup_logging("scheduler")


async def run() -> None:
    example_interval = settings.scheduler_example_interval_seconds
    due_scan_interval = settings.due_scan_interval_seconds
    logger.info(
        "scheduler started, example interval=%ss, due.scan interval=%ss",
        example_interval,
        due_scan_interval,
    )
    redis_client = create_redis_client()
    last_example = 0.0
    last_due_scan = 0.0
    try:
        while True:
            await heartbeat(redis_client, "scheduler")
            now = time.monotonic()
            if now - last_example >= example_interval:
                task = await enqueue(redis_client, "example.ping", {"source": "scheduler"})
                last_example = now
                logger.info("scheduler triggered example task: id=%s", task["id"])
            if now - last_due_scan >= due_scan_interval:
                task = await enqueue(redis_client, "due.scan", {"source": "scheduler"})
                last_due_scan = now
                logger.info("scheduler triggered due scan task: id=%s", task["id"])
            await asyncio.sleep(min(example_interval, due_scan_interval))
    finally:
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(run())
