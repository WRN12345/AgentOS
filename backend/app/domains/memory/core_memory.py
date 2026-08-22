"""核心记忆条目服务（设计文档第 8 节，M4.2）。

- 负责人手写条目（含新项目"种子记忆"，16.11）立即生效：proposed_by = confirmed_by = 负责人；
- 项目成员可读全部条目（含已作废，供追溯），可见提议者/确认者/生效时间；
- 容量预算：单项目生效条目合计约 4000 字符，超限拒绝并提示走整合精简（第 8 节）；
  预算逼大家只留真正重要的，而不是什么都往里堆；
- scope 本期固定 project，organization 为组织级预留（表结构已就位，接口层不接受）。

写操作（手写/作废）纳入审计域（16.10）。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiException, ErrorCodes
from app.domains.audit.service import record_event
from app.domains.memory.models import CoreMemoryEntry
from app.domains.project.models import ProjectMember
from app.domains.project.service import require_leader

#: 单项目核心记忆容量预算（生效条目合计字符数，设计文档第 8 节）
CORE_MEMORY_BUDGET_CHARS = 4000


async def list_entries(
    session: AsyncSession, *, project_id: uuid.UUID
) -> list[CoreMemoryEntry]:
    """项目全部核心记忆条目，生效在前（status 字典序 active < deprecated），按生效时间倒序。"""
    stmt = (
        select(CoreMemoryEntry)
        .where(CoreMemoryEntry.project_id == project_id)
        .order_by(CoreMemoryEntry.status, CoreMemoryEntry.effective_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def budget_usage(
    session: AsyncSession, *, project_id: uuid.UUID
) -> tuple[int, int]:
    """核心记忆容量占用：返回（生效条目合计字符数，预算上限）。供 M4.6 容量快满判断复用。"""
    stmt = select(CoreMemoryEntry.content).where(
        CoreMemoryEntry.project_id == project_id,
        CoreMemoryEntry.status == "active",
    )
    used = sum(len(content) for content in (await session.execute(stmt)).scalars().all())
    return used, CORE_MEMORY_BUDGET_CHARS


async def create_entry(
    session: AsyncSession, actor: ProjectMember, *, content: str
) -> CoreMemoryEntry:
    """负责人手写条目（种子记忆，16.11），立即生效。

    scope 固定 project（组织级预留，本期接口层不接受）；超容量预算拒绝并提示走整合。
    """
    require_leader(actor)
    content = content.strip()
    if not content:
        raise ApiException(400, ErrorCodes.VALIDATION_ERROR, "核心记忆内容不能为空")

    used, budget = await budget_usage(session, project_id=actor.project_id)
    if used + len(content) > budget:
        raise ApiException(
            400,
            ErrorCodes.CORE_MEMORY_BUDGET_EXCEEDED,
            "核心记忆容量不足，请先作废过时条目或走整合精简提议",
            details={"used": used, "budget": budget, "required": len(content)},
        )

    entry = CoreMemoryEntry(
        project_id=actor.project_id,
        scope="project",
        content=content,
        status="active",
        proposed_by_member_id=actor.id,
        confirmed_by_member_id=actor.id,
    )
    session.add(entry)
    await session.flush()
    await record_event(
        session,
        action="core_memory.created",
        actor_id=actor.user_id,
        target_type="core_memory_entry",
        target_id=entry.id,
        after={"content": content, "scope": "project", "source": "manual"},
        project_id=actor.project_id,
    )
    await session.commit()
    return entry


async def deprecate_entry(
    session: AsyncSession, actor: ProjectMember, *, entry_id: uuid.UUID
) -> CoreMemoryEntry:
    """负责人作废条目：保留供追溯，不再注入 Agent 上下文；跨项目访问按 404 处理。"""
    require_leader(actor)
    entry = await session.get(CoreMemoryEntry, entry_id)
    if entry is None or entry.project_id != actor.project_id:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "核心记忆条目不存在")
    if entry.status != "active":
        raise ApiException(
            409, ErrorCodes.CORE_MEMORY_INVALID_TRANSITION, "条目已作废，不能重复作废"
        )

    entry.status = "deprecated"
    await record_event(
        session,
        action="core_memory.deprecated",
        actor_id=actor.user_id,
        target_type="core_memory_entry",
        target_id=entry.id,
        before={"status": "active"},
        after={"status": "deprecated"},
        project_id=actor.project_id,
    )
    await session.commit()
    return entry
