"""Worker 承载 Agent 图运行（4.2 节，T5.2）与失败恢复（17.3 节，T5.6）。

消费 `agent.run` 队列任务：
1. agent_runs 置 running；
2. 用 AsyncPostgresSaver（psycopg 连接串由 DATABASE_URL 转换）把 LangGraph
   检查点持久化到 PostgreSQL，thread_id = run_id（重试重跑同一 thread，
   检查点机制保证不重复执行已完成节点，建议以 run 为单位不产生重复）；
3. 执行基础图；成功置 succeeded；失败按 17.3 节处理：
   - Schema 校验失败（SuggestionValidationError）是确定性错误，重试结果
     相同，直接终态 failed，不自动重试；
   - 其余错误（模型超时/不可用、图执行异常）按指数退避自动重试：
     retry_count+1、状态回 pending、经 ZSET 延迟队列重投，
     间隔 = AGENT_RUN_RETRY_BASE_SECONDS * 2^attempt；
   - 超过 AGENT_RUN_MAX_RETRIES 后终态 failed，错误信息留在
     agent_runs.error 可查，人工可经 POST /agent-runs/{id}/retry 重触发。

与 provider 层重试的关系：LLM_MAX_RETRIES 是单次模型调用内的线性重试
（应对瞬时抖动），本层是 run 粒度的第二道退避，两层不叠加放大——
provider 重试耗尽后错误才冒泡到本层。

检查点只做中断恢复，不替代 agent_runs/agent_suggestions 业务记录（原则 1）。
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
    """第 attempt 次（0 起）自动重试的退避间隔：base * 2^attempt（17.3 节）。"""
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
            # 幂等创建检查点表（checkpoints 等，不归 Alembic 管理）
            await checkpointer.setup()
            graph = build_agent_graph(checkpointer=checkpointer)
            final_state = await graph.ainvoke(
                initial_state(
                    run_id=run.id,
                    agent_type=run.agent_type,
                    trigger_source=run.trigger_source,
                    work_item_id=run.work_item_id,
                    request_id=run.request_id,
                    prompt=payload.get("prompt") or "",
                ),
                config={"configurable": {"thread_id": str(run.id)}},
            )
    except Exception as exc:  # 图内任何失败都在此兜底（17.3 节），不向上抛
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

    # 建议已落库后再发 SSE 事件（4.3 节："Agent 分析完成"触发建议中心刷新，T5.7）
    recipient = final_state.get("notification_recipient_id")
    if recipient:
        await publish_events(
            redis_client,
            [
                OutgoingEvent(
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
