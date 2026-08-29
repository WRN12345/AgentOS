"""Agent 运行触发、人工重试、建议查询与反馈服务。

request_agent_analysis() 创建 pending 的 agent_runs 记录并经 Redis 投递 agent.run，
由 worker 执行 LangGraph 基础图。

retry_agent_run() 人工重新触发失败的运行：状态重置为 pending、清空错误、
retry_count 清零后按原 agent_type/work_item_id/prompt 重新投递（run_id 不变，
检查点 thread_id 也不变，不会产生重复建议）。

scheduler 和 event 触发也复用 request_agent_analysis。人工反馈仅允许负责人处理
pending 建议，并记录 agent. 前缀审计事件。反馈不修改业务状态，唯一例外是接受
memory_proposal 时在同一事务写入核心记忆。
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

#: workers/worker.py 的 handle_task 按此类型分发。
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
    """创建 pending 的 agent_runs 记录并投递 agent.run，返回运行记录。

    worker 没有请求头，project_id 必须经队列载荷和 agent_runs.project_id 显式传递，
    不能从 X-Project-Id 推导。
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
        # 数据库记录已提交后若队列投递失败，必须立即终结，避免永久 pending 阻塞扫描。
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
    """人工重新触发失败的运行：重置为 pending 并重新投递。

    仅允许 failed 状态。retry_count 清零，为新一轮提供完整自动退避预算；run_id 和
    检查点 thread_id 保持不变，避免重复建议。prompt 使用 agent_runs 中的原始输入。
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
    """按类型、反馈状态和关联工作项过滤建议，按 created_at 倒序返回。

    关联工作项和 model 来自 agent_runs。建议通过 run 推导项目归属，
    agent_suggestions 不冗余 project_id。
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
    # 路由层已将实例放入 identity map。FOR UPDATE 等待期间其他事务可能提交新状态，
    # populate_existing 强制刷新，避免陈旧快照绕过 409 并重复采纳同一建议。
    locked_suggestion = (
        await session.execute(
            select(AgentSuggestion)
            .where(AgentSuggestion.id == suggestion.id)
            .with_for_update()
            .execution_options(populate_existing=True)
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

    # 拆解或分配反馈落定后尽力重建历史索引，使索引包含最新采纳状态。
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
    """拒绝对 accepted、ignored 或 expired 建议重复反馈。"""
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
