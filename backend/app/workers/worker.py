"""后台 Worker 入口。

Worker 与 API 共用 domains/ 与 infrastructure/ 的同一套领域模型和数据访问层，
从 Redis 队列消费后台任务（Agent 运行、到期提醒等）。

Worker 不得调用批准、转派或完成等业务命令，只允许生成建议或通知。
"""

import asyncio
import time

import redis.asyncio as redis

from app.core.config import settings
from app.core.logging import setup_logging

# 导入全部领域模型，确保 SQLAlchemy 跨领域外键（如 notifications.recipient_id
# → project_members.id）能在 Worker 进程内解析。
from app.agents import models as _agent_models  # noqa: F401
from app.domains.audit import models as _audit_models  # noqa: F401
from app.domains.collaboration import models as _collaboration_models  # noqa: F401
from app.domains.deadlines import models as _deadline_models  # noqa: F401
from app.domains.identity import models as _identity_models  # noqa: F401
from app.domains.memory import models as _memory_models  # noqa: F401
from app.domains.notifications import models as _notification_models  # noqa: F401
from app.domains.project import models as _project_models  # noqa: F401
from app.domains.transfers import models as _transfer_models  # noqa: F401
from app.domains.work_items import models as _work_item_models  # noqa: F401
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.queue.queue import dequeue, promote_due_delayed
from app.workers.agent_run import execute_agent_run
from app.workers.due_scan import scan_due_reminders
from app.workers.heartbeat import heartbeat
from app.workers.memory_index import execute_memory_index, recover_stale_file_indexes
from app.workers.memory_summary import execute_memory_summary
from app.workers.proposal_expire import expire_memory_proposals
from app.workers.risk_scan import run_risk_scan

logger = setup_logging("worker")


async def handle_task(task: dict, redis_client: redis.Redis) -> None:
    task_type = task.get("type", "<unknown>")
    if task_type == "example.ping":
        logger.info(
            "consumed example task: id=%s source=%s",
            task.get("id"),
            task.get("payload", {}).get("source", "<unknown>"),
        )
    elif task_type == "due.scan":
        # 提醒任务只写通知并发布事件，不得修改业务状态。
        await scan_due_reminders(redis_client)
    elif task_type == "agent.run":
        # Agent 图只产出建议和通知，失败仅记录在 `agent_runs`，不影响业务状态。
        await execute_agent_run(task.get("payload", {}), redis_client)
    elif task_type == "agent.risk_scan":
        # 风险扫描去重后投递 `agent.run`，不得修改业务状态。
        await run_risk_scan(redis_client)
    elif task_type == "memory.index":
        # 记忆索引失败时按退避策略重新入队。
        await execute_memory_index(task.get("payload", {}), redis_client)
    elif task_type == "memory.proposal_expire":
        # 挂起超过 7 天的核心记忆提议标记为 `expired`。
        await expire_memory_proposals()
    elif task_type == "memory.summary":
        # 经验总结在模型不可用时静默跳过，其他失败只记日志、不重试。
        await execute_memory_summary(task.get("payload", {}))
    else:
        logger.warning("unknown task type, skipped: id=%s type=%s", task.get("id"), task_type)


async def safe_handle_task(task: dict, redis_client: redis.Redis) -> None:
    """隔离单个任务的异常，避免拖垮 Worker。

    handle_task 内各处理器（如 execute_agent_run）已自行兜底业务异常；
    这里再兜住处理器自身的意外错误（如数据库瞬断），记录日志后继续
    消费后续任务，保证模型/Agent 故障不波及其他类型任务。
    """
    try:
        await handle_task(task, redis_client)
    except Exception:
        logger.exception(
            "task handling raised unexpectedly, worker continues: id=%s type=%s",
            task.get("id"),
            task.get("type", "<unknown>"),
        )


async def run() -> None:
    logger.info("worker started, waiting for tasks")
    redis_client = create_redis_client()
    last_file_index_recovery = 0.0
    try:
        while True:
            await heartbeat(redis_client, "worker")
            now = time.monotonic()
            if now - last_file_index_recovery >= settings.file_index_recovery_interval_seconds:
                try:
                    await recover_stale_file_indexes(redis_client)
                except Exception:  # noqa: BLE001 - 恢复扫描失败不能停止任务消费
                    logger.exception("stale file index recovery failed")
                last_file_index_recovery = now
            # 先恢复到期的延迟任务，避免即时队列持续繁忙时饿死重试任务。
            await promote_due_delayed(redis_client)
            task = await dequeue(redis_client, timeout=5)
            if task is not None:
                await safe_handle_task(task, redis_client)
    finally:
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(run())
