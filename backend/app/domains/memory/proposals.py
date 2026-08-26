"""核心记忆提议（设计文档第 8 节，M4.4）：Agent 提议、负责人确认生效。

- 提议复用 agent_suggestions 审批流（suggestion_type=memory_proposal），
  守住红线：确认前核心记忆无任何变化，Agent 只写建议不碰业务数据；
- 四类负载：create（新增）/ update（修改条目内容）/ deprecate（作废条目）/
  consolidate（整合精简：合并多条为一条，确认后旧条目作废、新条目生效，M4.6）；
- 确认（feedback=accepted）时才落到 core_memory_entries，与手写路径共用
  容量闸门与项目归属校验；校验失败则反馈整体回滚，建议保持 pending；
- 提议与确认均入审计域（16.10）：core_memory.proposed / created / updated / deprecated。
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

#: 核心记忆提议的建议类型（agent_suggestions.suggestion_type）
MEMORY_PROPOSAL_TYPE = "memory_proposal"

#: 提议动作类型
ProposalAction = Literal["create", "update", "deprecate", "consolidate"]


class MemoryProposalPayload(BaseModel):
    """提议负载（agent_suggestions.content）：动作 + 目标条目/新内容 + 理由。"""

    action: ProposalAction
    content: str | None = Field(default=None, max_length=CORE_MEMORY_BUDGET_CHARS)
    entry_id: uuid.UUID | None = None
    # consolidate：被合并作废的条目列表（≥2 条）
    entry_ids: list[uuid.UUID] | None = None
    reason: str | None = None


def _validate_payload(payload: MemoryProposalPayload) -> None:
    """动作与字段配套校验：create 要内容，update 要条目+内容，deprecate 要条目，
    consolidate 要 ≥2 条目标条目 + 精简后内容。"""
    if payload.action == "create":
        if not payload.content or not payload.content.strip():
            raise ApiException(400, ErrorCodes.VALIDATION_ERROR, "新增提议缺少内容")
    elif payload.action == "update":
        if payload.entry_id is None or not payload.content or not payload.content.strip():
            raise ApiException(400, ErrorCodes.VALIDATION_ERROR, "修改提议缺少目标条目或内容")
    elif payload.action == "deprecate":
        if payload.entry_id is None:
            raise ApiException(400, ErrorCodes.VALIDATION_ERROR, "作废提议缺少目标条目")
    else:  # consolidate
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
    """记录一条核心记忆提议（pending），等待负责人确认。

    提议本身不产生任何业务写入；项目归属经 run 推导（与既有建议一致）。
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
        actor_id=None,  # 提议者是 Agent，无成员身份
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
    """负责人确认提议（feedback=accepted）时落核心记忆，返回生效条目。

    在同一事务内执行、由调用方统一提交：任何校验失败（容量超限、条目已被
    抢先作废/修改、跨项目）都会让反馈整体回滚，建议保持 pending。
    """
    run = await session.get(AgentRun, suggestion.run_id)
    if run is None:  # FK 保证存在，防御性兜底
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
            proposed_by_member_id=None,  # Agent 提议
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
        # 整合精简（第 8 节，M4.6）：多条旧条目作废，合并为一条精简版本生效。
        # 旧条目先腾出预算再校验新内容；任一目标失效/跨项目则整体回滚。
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
            proposed_by_member_id=None,  # Agent 提议
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
        # 修改是替换：旧内容先腾出预算再校验新内容
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
    else:  # deprecate
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


# ---------- 提议自动过期（16.6，M4.5） ----------

#: 提议挂起上限（天）：超过无人确认自动过期，Agent 认为仍重要可重新提议
MEMORY_PROPOSAL_EXPIRE_DAYS = 7


async def expire_stale_proposals(
    session: AsyncSession, *, now: datetime | None = None
) -> int:
    """把挂起超 7 天的 memory_proposal 标记为 expired，返回过期条数。

    过期是终态：不占待确认列表（list 按 pending 过滤自然消失），
    再反馈走既有的非 pending → 409；审计留痕（16.10，actor 为空=系统动作）。
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
            actor_id=None,  # 系统动作
            target_type="agent_suggestion",
            target_id=suggestion.id,
            before={"review_status": "pending"},
            after={"review_status": "expired"},
            project_id=project_id,
        )
    await session.commit()
    return len(rows)
