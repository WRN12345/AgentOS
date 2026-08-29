"""项目核心记忆条目服务。

负责人手写条目会立即生效，项目成员可读取包含已作废条目在内的完整历史。
每个项目的生效内容受字符预算约束，并发写入通过锁定项目行串行校验。
接口只接受 `project` 范围，`organization` 仅为数据模型预留。写操作均记录审计事件。
"""

import uuid

import redis.asyncio as redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiException, ErrorCodes
from app.core.logging import setup_logging
from app.domains.audit.service import record_event
from app.domains.memory.indexer import MEMORY_INDEX_TASK_TYPE, MemoryIndexService
from app.domains.memory.models import (
    CORE_MEMORY_BUDGET_CHARS,
    CORE_MEMORY_NEAR_FULL_RATIO,
    CoreMemoryEntry,
)
from app.domains.memory.schemas import CoreMemoryEntryOut
from app.domains.project.models import Project, ProjectMember
from app.domains.project.service import require_leader
from app.domains.work_items.schemas import MemberBrief
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.queue.queue import enqueue

logger = setup_logging("backend")


async def list_entries(
    session: AsyncSession, *, project_id: uuid.UUID
) -> list[CoreMemoryEntry]:
    """返回项目全部条目，生效条目优先，同状态按生效时间倒序。"""
    stmt = (
        select(CoreMemoryEntry)
        .where(CoreMemoryEntry.project_id == project_id)
        .order_by(CoreMemoryEntry.status, CoreMemoryEntry.effective_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def budget_usage(
    session: AsyncSession, *, project_id: uuid.UUID
) -> tuple[int, int]:
    """返回生效条目的字符数总和及预算上限。"""
    stmt = select(CoreMemoryEntry.content).where(
        CoreMemoryEntry.project_id == project_id,
        CoreMemoryEntry.status == "active",
    )
    used = sum(len(content) for content in (await session.execute(stmt)).scalars().all())
    return used, CORE_MEMORY_BUDGET_CHARS


async def ensure_budget(
    session: AsyncSession, *, project_id: uuid.UUID, additional: int
) -> None:
    """校验加入 `additional` 字符后的核心记忆容量。

    手写创建和 `Agent` 提议确认共用此闸门，替换内容时 `additional` 可以为负。
    锁定项目行而不是条目集合，确保空集合下的并发插入也不能基于同一旧用量同时通过。
    """
    locked_project_id = await session.scalar(
        select(Project.id).where(Project.id == project_id).with_for_update()
    )
    if locked_project_id is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "项目不存在")
    used, budget = await budget_usage(session, project_id=project_id)
    if used + additional > budget:
        raise ApiException(
            400,
            ErrorCodes.CORE_MEMORY_BUDGET_EXCEEDED,
            "核心记忆容量不足，请先作废过时条目或走整合精简提议",
            details={"used": used, "budget": budget, "required": additional},
        )


async def budget_nearly_full(
    session: AsyncSession, *, project_id: uuid.UUID
) -> tuple[bool, int, int]:
    """返回容量是否接近上限、已用字符数和预算。"""
    used, budget = await budget_usage(session, project_id=project_id)
    return used >= budget * CORE_MEMORY_NEAR_FULL_RATIO, used, budget


async def entries_to_out(
    session: AsyncSession, entries: list[CoreMemoryEntry]
) -> list[CoreMemoryEntryOut]:
    """批量加载成员显示名并序列化条目；提议者为空表示由 `Agent` 提议。"""
    member_ids = {
        mid
        for e in entries
        for mid in (e.proposed_by_member_id, e.confirmed_by_member_id)
        if mid is not None
    }
    briefs: dict[uuid.UUID, MemberBrief] = {}
    if member_ids:
        members = (
            await session.execute(
                select(ProjectMember).where(ProjectMember.id.in_(member_ids))
            )
        ).scalars().all()
        briefs = {m.id: MemberBrief(id=m.id, display_name=m.display_name) for m in members}

    def brief_of(member_id: uuid.UUID) -> MemberBrief:
        return briefs.get(member_id) or MemberBrief(id=member_id, display_name="")

    return [
        CoreMemoryEntryOut(
            id=e.id,
            scope=e.scope,
            content=e.content,
            status=e.status,
            proposed_by=(
                brief_of(e.proposed_by_member_id) if e.proposed_by_member_id else None
            ),
            confirmed_by=brief_of(e.confirmed_by_member_id),
            effective_at=e.effective_at,
            created_at=e.created_at,
        )
        for e in entries
    ]


async def enqueue_core_memory_index_id(
    project_id: uuid.UUID, entry_id: uuid.UUID
) -> None:
    """投递核心记忆索引任务；`worker` 执行时读取条目的当前状态。"""
    redis_client: redis.Redis = create_redis_client()
    try:
        await enqueue(
            redis_client,
            MEMORY_INDEX_TASK_TYPE,
            {
                "project_id": str(project_id),
                "source_type": "core_memory",
                "source_id": str(entry_id),
            },
        )
    except Exception:  # noqa: BLE001 - 索引失败由 worker/后续重建处理，不阻塞业务写入
        logger.warning("core memory index task enqueue failed: entry=%s", entry_id, exc_info=True)
    finally:
        await redis_client.aclose()


async def enqueue_core_memory_index(entry: CoreMemoryEntry) -> None:
    await enqueue_core_memory_index_id(entry.project_id, entry.id)


async def invalidate_core_memory_index(
    session: AsyncSession, entry_id: uuid.UUID, *, commit: bool = True
) -> None:
    """作废核心记忆对应的向量块，保留块供追溯但排除检索。"""
    await MemoryIndexService(session).mark_source_stale(
        source_type="core_memory", source_id=entry_id, commit=commit
    )


async def create_entry(
    session: AsyncSession, actor: ProjectMember, *, content: str
) -> CoreMemoryEntry:
    """由负责人手写并立即生效一条核心记忆。

    `scope` 固定为 `project`；超出容量预算时拒绝写入。
    """
    require_leader(actor)
    content = content.strip()
    if not content:
        raise ApiException(400, ErrorCodes.VALIDATION_ERROR, "核心记忆内容不能为空")

    await ensure_budget(session, project_id=actor.project_id, additional=len(content))

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
    await enqueue_core_memory_index(entry)
    return entry


async def deprecate_entry(
    session: AsyncSession, actor: ProjectMember, *, entry_id: uuid.UUID
) -> CoreMemoryEntry:
    """作废条目并保留追溯记录，不再注入 `Agent` 上下文。

    跨项目访问按条目不存在处理。
    """
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
    await invalidate_core_memory_index(session, entry.id, commit=False)
    await session.commit()
    return entry
