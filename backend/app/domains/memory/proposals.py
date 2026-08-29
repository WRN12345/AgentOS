"""由 `Agent` 提议、项目负责人确认生效的核心记忆工作流。

提议复用 `agent_suggestions` 审批流，确认前不得修改核心记忆。支持新增、替换、作废和
合并精简；只有负责人接受后才写入 `core_memory_entries`。确认与反馈处于同一事务，
容量、状态或项目归属校验失败会整体回滚，提议保持待确认。提议和确认动作均记录审计事件。
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.models import AgentRun, AgentSuggestion
from app.core.errors import ApiException, ErrorCodes
from app.domains.audit.service import record_event
from app.domains.memory.core_memory import ensure_budget, invalidate_core_memory_index
from app.domains.memory.models import CORE_MEMORY_BUDGET_CHARS, CoreMemoryEntry
from app.domains.project.models import ProjectMember

MEMORY_PROPOSAL_TYPE = "memory_proposal"

ProposalAction = Literal["create", "update", "deprecate", "consolidate"]


class MemoryProposalPayload(BaseModel):
    """存入 `agent_suggestions.content` 的核心记忆提议负载。"""

    action: ProposalAction
    content: str | None = Field(default=None, max_length=CORE_MEMORY_BUDGET_CHARS)
    entry_id: uuid.UUID | None = None
    entry_ids: list[uuid.UUID] | None = None
    reason: str | None = None


def _validate_payload(payload: MemoryProposalPayload) -> None:
    """校验每种提议动作所需的目标条目和内容。"""
    if payload.action == "create":
        if not payload.content or not payload.content.strip():
            raise ApiException(400, ErrorCodes.VALIDATION_ERROR, "新增提议缺少内容")
    elif payload.action == "update":
        if payload.entry_id is None or not payload.content or not payload.content.strip():
            raise ApiException(400, ErrorCodes.VALIDATION_ERROR, "修改提议缺少目标条目或内容")
    elif payload.action == "deprecate":
        if payload.entry_id is None:
            raise ApiException(400, ErrorCodes.VALIDATION_ERROR, "作废提议缺少目标条目")
    else:
        if not payload.entry_ids or len(set(payload.entry_ids)) < 2:
            raise ApiException(
                400, ErrorCodes.VALIDATION_ERROR, "整合提议至少需要两条不同的目标条目"
            )
        if not payload.content or not payload.content.strip():
            raise ApiException(400, ErrorCodes.VALIDATION_ERROR, "整合提议缺少精简后的内容")


async def create_memory_proposal(
    session: AsyncSession,
    *,
    run: AgentRun,
    action: ProposalAction,
    content: str | None = None,
    entry_id: uuid.UUID | None = None,
    entry_ids: list[uuid.UUID] | None = None,
    reason: str | None = None,
) -> AgentSuggestion:
    """记录一条待负责人确认的核心记忆提议。

    提议本身不修改核心记忆，项目归属只能从 `run` 推导。
    """
    payload = MemoryProposalPayload(
        action=action, content=content, entry_id=entry_id, entry_ids=entry_ids, reason=reason
    )
    _validate_payload(payload)

    suggestion = AgentSuggestion(
        run_id=run.id,
        suggestion_type=MEMORY_PROPOSAL_TYPE,
        content=payload.model_dump(mode="json"),
    )
    session.add(suggestion)
    await session.flush()
    await record_event(
        session,
        action="core_memory.proposed",
        actor_id=None,
        target_type="agent_suggestion",
        target_id=suggestion.id,
        after=payload.model_dump(mode="json"),
        project_id=run.project_id,
    )
    await session.commit()
    return suggestion


async def apply_memory_proposal(
    session: AsyncSession,
    suggestion: AgentSuggestion,
    *,
    confirmer: ProjectMember,
) -> CoreMemoryEntry:
    """负责人接受提议时修改核心记忆并返回目标条目。

    调用方负责统一提交事务。容量超限、并发状态变化或跨项目访问会使反馈和记忆修改
    整体回滚，提议继续保持待确认。
    """
    run = await session.get(AgentRun, suggestion.run_id)
    if run is None:  # 外键应保证存在，此处保留防御性校验
        raise ApiException(404, ErrorCodes.NOT_FOUND, "Agent 运行不存在")
    project_id = run.project_id

    try:
        payload = MemoryProposalPayload.model_validate(suggestion.content)
        _validate_payload(payload)
    except ValidationError:
        raise ApiException(400, ErrorCodes.VALIDATION_ERROR, "提议负载格式无效") from None

    audit_base = {"source": "agent_proposal", "proposal_id": str(suggestion.id)}
    if payload.action == "create":
        content = (payload.content or "").strip()
        await ensure_budget(session, project_id=project_id, additional=len(content))
        entry = CoreMemoryEntry(
            project_id=project_id,
            scope="project",
            content=content,
            status="active",
            proposed_by_member_id=None,
            confirmed_by_member_id=confirmer.id,
        )
        session.add(entry)
        await session.flush()
        await record_event(
            session,
            action="core_memory.created",
            actor_id=confirmer.user_id,
            target_type="core_memory_entry",
            target_id=entry.id,
            after={"content": content, "scope": "project", **audit_base},
            project_id=project_id,
        )
        return entry

    if payload.action == "consolidate":
        # 先释放旧条目预算再校验合并内容；目标失效或跨项目时整体回滚
        content = (payload.content or "").strip()
        targets = (
            (
                await session.execute(
                    select(CoreMemoryEntry).where(
                        CoreMemoryEntry.id.in_(payload.entry_ids or [])
                    )
                )
            )
            .scalars()
            .all()
        )
        if len(targets) != len(set(payload.entry_ids or [])) or any(
            t.project_id != project_id for t in targets
        ):
            raise ApiException(404, ErrorCodes.NOT_FOUND, "整合目标条目不存在")
        if any(t.status != "active" for t in targets):
            raise ApiException(
                409,
                ErrorCodes.CORE_MEMORY_INVALID_TRANSITION,
                "整合目标中有条目已作废，提议需重新评估后再提",
            )
        freed = sum(len(t.content) for t in targets)
        await ensure_budget(session, project_id=project_id, additional=len(content) - freed)
        for t in targets:
            t.status = "deprecated"
            await invalidate_core_memory_index(session, t.id, commit=False)
            await record_event(
                session,
                action="core_memory.deprecated",
                actor_id=confirmer.user_id,
                target_type="core_memory_entry",
                target_id=t.id,
                before={"status": "active"},
                after={"status": "deprecated", **audit_base},
                project_id=project_id,
            )
        merged = CoreMemoryEntry(
            project_id=project_id,
            scope="project",
            content=content,
            status="active",
            proposed_by_member_id=None,
            confirmed_by_member_id=confirmer.id,
        )
        session.add(merged)
        await session.flush()
        await record_event(
            session,
            action="core_memory.created",
            actor_id=confirmer.user_id,
            target_type="core_memory_entry",
            target_id=merged.id,
            after={
                "content": content,
                "scope": "project",
                "consolidates": [str(t.id) for t in targets],
                **audit_base,
            },
            project_id=project_id,
        )
        return merged

    target = await session.get(CoreMemoryEntry, payload.entry_id)
    if target is None or target.project_id != project_id:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "核心记忆条目不存在")
    if target.status != "active":
        raise ApiException(
            409,
            ErrorCodes.CORE_MEMORY_INVALID_TRANSITION,
            "目标条目已作废，提议需重新评估后再提",
        )

    if payload.action == "update":
        content = (payload.content or "").strip()
        # 替换时按新旧内容差额校验容量
        await ensure_budget(
            session, project_id=project_id, additional=len(content) - len(target.content)
        )
        before = {"content": target.content}
        target.content = content
        target.confirmed_by_member_id = confirmer.id
        target.effective_at = datetime.now(UTC)
        await record_event(
            session,
            action="core_memory.updated",
            actor_id=confirmer.user_id,
            target_type="core_memory_entry",
            target_id=target.id,
            before=before,
            after={"content": content, **audit_base},
            project_id=project_id,
        )
    else:
        target.status = "deprecated"
        await invalidate_core_memory_index(session, target.id, commit=False)
        await record_event(
            session,
            action="core_memory.deprecated",
            actor_id=confirmer.user_id,
            target_type="core_memory_entry",
            target_id=target.id,
            before={"status": "active"},
            after={"status": "deprecated", **audit_base},
            project_id=project_id,
        )
    await session.flush()
    return target


#: 待确认提议超过此期限后自动过期，`Agent` 可以重新提议
MEMORY_PROPOSAL_EXPIRE_DAYS = 7


async def expire_stale_proposals(
    session: AsyncSession, *, now: datetime | None = None
) -> int:
    """将超过七天的待确认提议标记为 `expired`，并返回数量。

    过期是终态，不再出现在待确认列表中；后续反馈会因状态冲突被拒绝。
    过期动作记录审计事件，`actor_id` 为空表示系统动作。
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=MEMORY_PROPOSAL_EXPIRE_DAYS)
    stmt = (
        select(AgentSuggestion, AgentRun.project_id)
        .join(AgentRun, AgentSuggestion.run_id == AgentRun.id)
        .where(
            AgentSuggestion.suggestion_type == MEMORY_PROPOSAL_TYPE,
            AgentSuggestion.review_status == "pending",
            AgentSuggestion.created_at < cutoff,
        )
    )
    rows = (await session.execute(stmt)).all()
    for suggestion, project_id in rows:
        suggestion.review_status = "expired"
        suggestion.reviewed_at = now
        await record_event(
            session,
            action="core_memory.proposal_expired",
            actor_id=None,
            target_type="agent_suggestion",
            target_id=suggestion.id,
            before={"review_status": "pending"},
            after={"review_status": "expired"},
            project_id=project_id,
        )
    await session.commit()
    return len(rows)
