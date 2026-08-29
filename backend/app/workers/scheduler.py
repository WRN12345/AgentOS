"""Scheduler 入口。

按配置周期触发后台任务，实际执行均由 Worker 消费：

- `example.ping`：工程链路示例任务（SCHEDULER_EXAMPLE_INTERVAL_SECONDS）；
- `due.scan`：到期或逾期提醒扫描，默认每 300 秒触发；
- `agent.risk_scan`：Workflow Risk Agent 周期风险扫描
  默认每 24 小时触发；
- `memory.proposal_expire`：核心记忆提议过期扫描
  默认每 24 小时触发。

单循环记录各任务的上次触发时间，到达周期即入队，睡眠时间取所有周期的最小值。
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
    risk_scan_interval = settings.agent_risk_scan_interval_seconds
    proposal_expire_interval = settings.memory_proposal_expire_interval_seconds
    logger.info(
        "scheduler started, example interval=%ss, due.scan interval=%ss, agent.risk_scan interval=%ss, memory.proposal_expire interval=%ss",
        example_interval,
        due_scan_interval,
        risk_scan_interval,
        proposal_expire_interval,
    )
    redis_client = create_redis_client()
    last_example = 0.0
    last_due_scan = 0.0
    last_risk_scan = 0.0
    last_proposal_expire = 0.0
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
            if now - last_risk_scan >= risk_scan_interval:
                task = await enqueue(redis_client, "agent.risk_scan", {"source": "scheduler"})
                last_risk_scan = now
                logger.info("scheduler triggered risk scan task: id=%s", task["id"])
            if now - last_proposal_expire >= proposal_expire_interval:
                task = await enqueue(
                    redis_client, "memory.proposal_expire", {"source": "scheduler"}
                )
                last_proposal_expire = now
                logger.info("scheduler triggered proposal expire task: id=%s", task["id"])
            await asyncio.sleep(
                min(
                    example_interval,
                    due_scan_interval,
                    risk_scan_interval,
                    proposal_expire_interval,
                )
            )
    finally:
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(run())
