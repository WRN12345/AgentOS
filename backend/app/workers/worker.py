"""后台 Worker 入口（4.2 节）。

Worker 与 API 共用 domains/ 与 infrastructure/ 的同一套领域模型和数据访问层，
从 Redis 队列消费后台任务（Agent 运行、到期提醒等）。

约束（4.2 节）：Worker 不得调用"批准、转派、完成"等业务命令，
只允许生成建议或通知。本骨架中不引入任何业务命令代码路径。
"""

import asyncio

import redis.asyncio as redis

from app.core.logging import setup_logging

# 导入全部领域模型，确保 SQLAlchemy 跨领域外键（如 notifications.recipient_id
# → project_members.id）在 worker 进程内可解析（与 migrations/env.py 同一模式）
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
from app.workers.memory_index import execute_memory_index
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
        # 到期/逾期提醒扫描（T3.6）：只写通知 + 发布事件，不触碰业务状态（4.2 硬约束）
        await scan_due_reminders(redis_client)
    elif task_type == "agent.run":
        # Agent 图运行（T5.2）：产出建议与通知，失败只标记 agent_runs，不影响业务
        await execute_agent_run(task.get("payload", {}), redis_client)
    elif task_type == "agent.risk_scan":
        # Workflow Risk Agent 周期风险扫描（T5.5）：去重后投递 agent.run，不触碰业务状态
        await run_risk_scan(redis_client)
    elif task_type == "memory.index":
        # 记忆索引任务（M1.8）：切块→embedding→memory_chunks，失败按退避重入队
        await execute_memory_index(task.get("payload", {}), redis_client)
    else:
        logger.warning("unknown task type, skipped: id=%s type=%s", task.get("id"), task_type)


async def safe_handle_task(task: dict, redis_client: redis.Redis) -> None:
    """单个任务的处理异常不拖垮 worker（第 22 章标准 9，T5.6）。

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
    try:
        while True:
            await heartbeat(redis_client, "worker")
            # 先把到点的延迟任务（T5.6 退避重试）搬回即时队列
            await promote_due_delayed(redis_client)
            task = await dequeue(redis_client, timeout=5)
            if task is not None:
                await safe_handle_task(task, redis_client)
    finally:
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(run())
