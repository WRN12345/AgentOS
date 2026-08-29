"""Worker 中的 Agent 图运行与失败恢复。

消费 `agent.run` 队列任务：
运行状态先置为 `running`。LangGraph 检查点通过 `AsyncPostgresSaver` 持久化到
PostgreSQL，并以 `run_id` 作为 `thread_id`，使重试沿用同一线程且不重复执行已完成
节点。成功后状态置为 `succeeded`。

`SuggestionValidationError` 是确定性错误，直接置为 `failed`。模型超时、服务不可用
或图执行异常按运行粒度指数退避，经 ZSET 延迟重投；重试耗尽后错误保存在
`agent_runs.error`，可由人工接口重新触发。

与 provider 层重试的关系：LLM_MAX_RETRIES 是单次模型调用内的线性重试
（应对瞬时抖动），本层是 run 粒度的第二道退避，两层不叠加放大——
provider 重试耗尽后错误才冒泡到本层。

检查点只用于中断恢复，不替代 `agent_runs` 和 `agent_suggestions` 业务记录。
"""

import time
import uuid

import redis.asyncio as redis
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.agents.graphs.base import build_agent_graph, initial_state
from app.agents.models import AgentRun
from app.agents.schemas.suggestion import SuggestionValidationError
from app.agents.service import AGENT_RUN_TASK_TYPE
from app.core.config import settings
from app.core.logging import setup_logging
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.events import OutgoingEvent, publish_events
from app.infrastructure.queue.queue import enqueue_delayed

logger = setup_logging("worker")


def _checkpoint_dsn() -> str:
    """DATABASE_URL（asyncpg）→ psycopg 连接串（仅驱动段不同，两者并存无冲突）。"""
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def retry_delay_seconds(attempt: int) -> float:
    """计算第 `attempt` 次自动重试的指数退避间隔，计数从 0 开始。"""
    return settings.agent_run_retry_base_seconds * (2**attempt)


def is_retryable_error(exc: Exception) -> bool:
    """错误是否值得自动重试。

    SuggestionValidationError（输出结构校验失败）是确定性错误——同一输入
    重试结果相同，直接 failed；其余（模型超时/不可用、图执行异常等）
    视为可重试的瞬时/环境错误。
    """
    return not isinstance(exc, SuggestionValidationError)


async def execute_agent_run(payload: dict, redis_client: redis.Redis) -> AgentRun | None:
    """执行一次 Agent 运行；返回最终 agent_runs 记录（不存在则 None）。"""
    run_id = uuid.UUID(payload["run_id"])
    started = time.monotonic()

    async with async_session_factory() as session:
        run = await session.get(AgentRun, run_id)
        if run is None:
            logger.warning("agent run not found, skipped: run_id=%s", run_id)
            return None
        run.status = "running"
        await session.commit()

    final_state: dict = {}
    try:
        async with AsyncPostgresSaver.from_conn_string(_checkpoint_dsn()) as checkpointer:
            # 检查点表不归 Alembic 管理，因此在运行时幂等创建。
            await checkpointer.setup()
            graph = build_agent_graph(checkpointer=checkpointer)
            final_state = await graph.ainvoke(
                initial_state(
                    run_id=run.id,
                    agent_type=run.agent_type,
                    trigger_source=run.trigger_source,
                    project_id=run.project_id,
                    work_item_id=run.work_item_id,
                    request_id=run.request_id,
                    prompt=payload.get("prompt") or "",
                ),
                config={"configurable": {"thread_id": str(run.id)}},
            )
    except Exception as exc:  # 图内任何失败都在此兜底，不向上抛
        logger.exception("agent run failed: run_id=%s", run_id)
        retry_delay: float | None = None
        async with async_session_factory() as session:
            run = await session.get(AgentRun, run_id)
            assert run is not None
            run.error = f"{type(exc).__name__}: {exc}"[:2000]
            run.duration_ms = int((time.monotonic() - started) * 1000)
            if is_retryable_error(exc) and run.retry_count < settings.agent_run_max_retries:
                # 可重试错误且未超上限：回 pending 并延迟重投，等待下次执行
                retry_delay = retry_delay_seconds(run.retry_count)
                run.retry_count += 1
                run.status = "pending"
            else:
                # 确定性错误，或重试耗尽：终态 failed，错误留档待人工重触发
                run.status = "failed"
            await session.commit()
        if retry_delay is not None:
            await enqueue_delayed(
                redis_client, AGENT_RUN_TASK_TYPE, payload, delay_seconds=retry_delay
            )
            logger.info(
                "agent run retry scheduled: run_id=%s retry_count=%s delay=%ss",
                run_id,
                run.retry_count,
                retry_delay,
            )
        return run

    async with async_session_factory() as session:
        run = await session.get(AgentRun, run_id)
        assert run is not None
        run.status = "succeeded"
        run.duration_ms = int((time.monotonic() - started) * 1000)
        await session.commit()

    # 建议提交成功后再发布 SSE，确保客户端刷新时能读取到对应记录。
    recipient = final_state.get("notification_recipient_id")
    project_id = final_state.get("notification_project_id")
    if recipient and project_id:
        await publish_events(
            redis_client,
            [
                OutgoingEvent(
                    project_id=uuid.UUID(project_id),
                    recipient_id=uuid.UUID(recipient),
                    type="agent.suggestion_ready",
                    title=final_state.get("notification_title", "Agent 分析完成"),
                    body=final_state.get("notification_body", ""),
                    link=final_state.get("notification_link"),
                )
            ],
        )
    logger.info(
        "agent run succeeded: run_id=%s duration_ms=%s", run_id, run.duration_ms
    )
    return run
