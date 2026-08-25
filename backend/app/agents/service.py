"""Agent 运行触发入口（T5.2）与人工重试（T5.6，17.3 节）。

request_agent_analysis()：创建 agent_runs 记录（pending）并经 Redis 队列投递
`agent.run` 任务（复用 T1.6 队列机制），由 worker 消费执行 LangGraph 基础图。

retry_agent_run()：人工重新触发失败的运行——状态重置为 pending、清空错误、
retry_count 清零后按原 agent_type/work_item_id/prompt 重新投递（run_id 不变，
检查点 thread_id 也不变，不会产生重复建议）。

T5.4 已接到正式 API `POST /work-items/{id}/agent-analysis`（12.5 节）；
T5.5 的 scheduler/event 触发复用 request_agent_analysis。

T5.7（12.5 节）：list_suggestions() 建议查询（join agent_runs 补
work_item_id/model）；submit_suggestion_feedback() 人工采纳/忽略反馈
（仅负责人、仅 pending，写 agent. 前缀审计事件，不触碰任何业务状态；
唯一例外：memory_proposal 被采纳时同事务落入核心记忆，M4.4）。
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.models import AgentRun, AgentSuggestion
from app.core.config import settings
from app.core.errors import ApiException, ErrorCodes
from app.core.logging import setup_logging
from app.domains.audit.service import record_event
from app.domains.memory.history import HISTORY_RUN_AGENT_TYPES, enqueue_run_history_index
from app.domains.memory.core_memory import enqueue_core_memory_index, enqueue_core_memory_index_id
from app.domains.memory.proposals import MEMORY_PROPOSAL_TYPE, MemoryProposalPayload, apply_memory_proposal
from app.domains.project.models import ProjectMember
from app.infrastructure.queue.queue import enqueue

logger = setup_logging("backend")

#: worker 消费的任务类型（workers/worker.py handle_task 分发）
AGENT_RUN_TASK_TYPE = "agent.run"


async def request_agent_analysis(
    session: AsyncSession,
    redis_client: redis.Redis,
    *,
    agent_type: str,
    project_id: uuid.UUID,
    trigger_source: str = "manual",
    work_item_id: uuid.UUID | None = None,
    prompt: str = "",
    request_id: str | None = None,
) -> AgentRun:
    """创建一条 agent_runs(pending) 并投递 agent.run 队列任务，返回运行记录。

    project_id 显式传参：worker 进程无请求头，项目上下文必须经队列载荷 +
    agent_runs.project_id 传递，绝不靠 X-Project-Id 推导（ticket 05 硬约束）。
    """
    run = AgentRun(
        status="pending",
        agent_type=agent_type,
        model=settings.llm_model or None,
        trigger_source=trigger_source,
        work_item_id=work_item_id,
        project_id=project_id,
        prompt=prompt,
        request_id=request_id,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)

    try:
        task = await enqueue(
            redis_client,
            AGENT_RUN_TASK_TYPE,
            {
                "run_id": str(run.id),
                "agent_type": agent_type,
                "project_id": str(project_id),
                "work_item_id": str(work_item_id) if work_item_id else None,
                "prompt": prompt,
                "request_id": request_id,
            },
        )
    except Exception as exc:
        # 记录已提交但队列投递失败时立即终结，避免永久 pending 阻塞周期扫描。
        run.status = "failed"
        run.error = f"Queue dispatch failed: {type(exc).__name__}: {exc}"[:2000]
        await session.commit()
        raise
    logger.info("enqueued agent run: run_id=%s task_id=%s type=%s", run.id, task["id"], agent_type)
    return run


async def retry_agent_run(
    session: AsyncSession,
    redis_client: redis.Redis,
    run: AgentRun,
) -> AgentRun:
    """人工重新触发失败的运行（17.3 节，T5.6）：重置为 pending 并重新投递。

    仅允许 failed 状态（路由层校验）。retry_count 清零：人工重触发即承认此前
    自动退避已耗尽，给新一轮完整退避预算；累计值当前无消费方，历史错误由
    日志可查，故不保留。run_id 不变（检查点 thread_id 不变），重跑同一 run
    不产生重复建议。prompt 取 agent_runs 持久化的原输入。
    """
    run.status = "pending"
    run.error = None
    run.duration_ms = None
    run.retry_count = 0
    await session.commit()
    await session.refresh(run)

    task = await enqueue(
        redis_client,
        AGENT_RUN_TASK_TYPE,
        {
            "run_id": str(run.id),
            "agent_type": run.agent_type,
            "project_id": str(run.project_id) if run.project_id else None,
            "work_item_id": str(run.work_item_id) if run.work_item_id else None,
            "prompt": run.prompt,
            "request_id": run.request_id,
        },
    )
    logger.info("agent run manually retried: run_id=%s task_id=%s", run.id, task["id"])
    return run


async def list_suggestions(
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None = None,
    suggestion_type: str | None = None,
    review_status: str | None = None,
    work_item_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[tuple[AgentSuggestion, AgentRun]]:
    """按类型/反馈状态/关联工作项过滤建议（created_at 倒序）。

    join agent_runs：关联工作项挂在 run 上（项目级建议为 NULL），
    列表出参同时需要 run 的 model 供前端展示。
    project_id 必填（路由层传 actor.project_id）：建议经 run 推导归属，
    agent_suggestions 不冗余 project_id（ticket 05）。
    """
    stmt = (
        select(AgentSuggestion, AgentRun)
        .join(AgentRun, AgentSuggestion.run_id == AgentRun.id)
        .order_by(AgentSuggestion.created_at.desc(), AgentSuggestion.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if project_id is not None:
        stmt = stmt.where(AgentRun.project_id == project_id)
    if suggestion_type:
        stmt = stmt.where(AgentSuggestion.suggestion_type == suggestion_type)
    if review_status:
        stmt = stmt.where(AgentSuggestion.review_status == review_status)
    if work_item_id is not None:
        stmt = stmt.where(AgentRun.work_item_id == work_item_id)
    return list((await session.execute(stmt)).all())


async def submit_suggestion_feedback(
    session: AsyncSession,
    suggestion: AgentSuggestion,
    *,
    action: str,
    member: ProjectMember,
) -> AgentSuggestion:
    """原子写入人工采纳结果，确保一条建议只会被处理一次。

    以 ``SELECT ... FOR UPDATE`` 重新读取建议行。状态检查、memory_proposal
    的核心记忆副作用、审计事件和状态写入都在该锁覆盖的同一事务中，因此并发
    accepted/ignored 或请求重放只能有一个请求成功；其余请求在获得锁后返回 409。
    """
    locked_suggestion = (
        await session.execute(
            select(AgentSuggestion)
            .where(AgentSuggestion.id == suggestion.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if locked_suggestion is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "Agent 建议不存在")
    raise_if_suggestion_reviewed(locked_suggestion)

    applied_entry = None
    proposal_payload = None
    if action == "accepted" and locked_suggestion.suggestion_type == MEMORY_PROPOSAL_TYPE:
        applied_entry = await apply_memory_proposal(
            session, locked_suggestion, confirmer=member
        )
        proposal_payload = MemoryProposalPayload.model_validate(locked_suggestion.content)

    before: dict[str, Any] = {"review_status": locked_suggestion.review_status}
    locked_suggestion.review_status = action
    locked_suggestion.reviewed_by = member.id
    locked_suggestion.reviewed_at = datetime.now(UTC)
    await record_event(
        session,
        action="agent.suggestion_feedback",
        actor_id=member.id,
        target_type="agent_suggestion",
        target_id=locked_suggestion.id,
        before=before,
        after={"review_status": action},
    )
    await session.commit()
    await session.refresh(locked_suggestion)

    if applied_entry is not None and proposal_payload is not None:
        await enqueue_core_memory_index(applied_entry)
        if proposal_payload.action == "consolidate":
            run = await session.get(AgentRun, locked_suggestion.run_id)
            if run is not None:
                for entry_id in proposal_payload.entry_ids or []:
                    await enqueue_core_memory_index_id(run.project_id, entry_id)

    # M5.1：拆解/分配运行的建议反馈落定后，重投历史索引任务（整体重建，
    # 块内容反映最新采纳状态）；best-effort，投递失败只记日志
    run = await session.get(AgentRun, locked_suggestion.run_id)
    if run is not None and run.agent_type in HISTORY_RUN_AGENT_TYPES:
        await enqueue_run_history_index(run)

    logger.info(
        "agent suggestion feedback: id=%s action=%s actor=%s",
        locked_suggestion.id,
        action,
        member.id,
    )
    return locked_suggestion


def raise_if_suggestion_reviewed(suggestion: AgentSuggestion) -> None:
    """状态迁移校验：已终结（accepted/ignored/expired）的建议再次反馈 → 409。"""
    if suggestion.review_status != "pending":
        raise ApiException(
            409,
            ErrorCodes.AGENT_SUGGESTION_ALREADY_REVIEWED,
            "该建议已完成人工反馈或已过期，不可重复反馈",
            details={
                "suggestion_id": str(suggestion.id),
                "review_status": suggestion.review_status,
            },
        )
