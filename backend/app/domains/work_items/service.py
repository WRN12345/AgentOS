"""工作项应用服务与权限策略（4.1、6.1、7.1、12.3 节）。

权限规则（16 节，每个用例显式校验）：
- 创建、修改、发布、取消：仅项目负责人；
- start / block / unblock / submit：仅当前主执行人（原则 4 主责任唯一）；
- 查询：任何项目成员（原则 6 透明）。
状态迁移由 domains/work_items/state_machine.py 裁决（8.1 节）；
每次状态迁移/字段变更与同事务写审计事件（原则 5），assignee 变化必留痕。
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import ApiException, ErrorCodes
from app.core.logging import setup_logging
from app.domains.audit.service import record_event
from app.domains.project.models import ROLE_LEADER, ProjectMember
from app.domains.work_items.models import WorkItem, WorkItemCollaborator
from app.domains.work_items.schemas import (
    MemberBrief,
    WorkItemCreateIn,
    WorkItemOut,
    WorkItemSummaryOut,
    WorkItemUpdateIn,
)
from app.domains.work_items.state_machine import transition

logger = setup_logging("backend")

# 命令 → 审计动作名
_COMMAND_AUDIT_ACTION = {
    "publish": "work_item.published",
    "start": "work_item.started",
    "block": "work_item.blocked",
    "unblock": "work_item.unblocked",
    "submit": "work_item.submitted",
    "cancel": "work_item.cancelled",
}

# 命令 → 触发者要求："leader" 仅负责人，"assignee" 仅当前主执行人
_COMMAND_ACTOR = {
    "publish": "leader",
    "start": "assignee",
    "block": "assignee",
    "unblock": "assignee",
    "submit": "assignee",
    "cancel": "leader",
}


# ---------- 查询 ----------


async def get_work_item(session: AsyncSession, item_id: uuid.UUID) -> WorkItem:
    stmt = (
        select(WorkItem)
        .where(WorkItem.id == item_id)
        .options(selectinload(WorkItem.collaborators))  # 预加载，避免异步懒加载
    )
    item = (await session.execute(stmt)).scalar_one_or_none()
    if item is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "工作项不存在")
    return item


async def list_work_items(
    session: AsyncSession,
    *,
    assignee_id: uuid.UUID | None = None,
    status: str | None = None,
    due_from: datetime | None = None,
    due_to: datetime | None = None,
) -> list[WorkItemSummaryOut]:
    """全量列表（原则 6 透明），支持按负责人、状态、DDL 区间过滤（13.1 节）。"""
    stmt = select(WorkItem).order_by(WorkItem.created_at.desc())
    if assignee_id is not None:
        stmt = stmt.where(WorkItem.assignee_id == assignee_id)
    if status is not None:
        stmt = stmt.where(WorkItem.status == status)
    if due_from is not None:
        stmt = stmt.where(WorkItem.due_at >= due_from)
    if due_to is not None:
        stmt = stmt.where(WorkItem.due_at <= due_to)
    items = list((await session.execute(stmt)).scalars().all())
    briefs = await _member_briefs(session, items)
    return [_to_summary(item, briefs) for item in items]


# ---------- 序列化 ----------


async def _member_briefs(
    session: AsyncSession, items: list[WorkItem]
) -> dict[uuid.UUID, MemberBrief]:
    """批量取主执行人与协作者的显示名。"""
    member_ids: set[uuid.UUID] = set()
    for item in items:
        member_ids.add(item.assignee_id)
        member_ids.update(c.member_id for c in item.collaborators)
    if not member_ids:
        return {}
    stmt = select(ProjectMember).where(ProjectMember.id.in_(member_ids))
    members = (await session.execute(stmt)).scalars().all()
    return {m.id: MemberBrief(id=m.id, display_name=m.display_name) for m in members}


def _brief(briefs: dict[uuid.UUID, MemberBrief], member_id: uuid.UUID) -> MemberBrief:
    return briefs.get(member_id) or MemberBrief(id=member_id, display_name="")


def _to_summary(item: WorkItem, briefs: dict[uuid.UUID, MemberBrief]) -> WorkItemSummaryOut:
    return WorkItemSummaryOut(
        id=item.id,
        title=item.title,
        status=item.status,
        priority=item.priority,
        assignee=_brief(briefs, item.assignee_id),
        due_at=item.due_at,
        version=item.version,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


async def work_item_to_out(session: AsyncSession, item: WorkItem) -> WorkItemOut:
    briefs = await _member_briefs(session, [item])
    return WorkItemOut(
        id=item.id,
        title=item.title,
        description=item.description,
        acceptance_criteria=item.acceptance_criteria,
        priority=item.priority,
        status=item.status,
        assignee=_brief(briefs, item.assignee_id),
        collaborators=[_brief(briefs, c.member_id) for c in item.collaborators],
        due_at=item.due_at,
        version=item.version,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


# ---------- 内部工具 ----------


def _check_version(item: WorkItem, version: int) -> None:
    """乐观锁（17.2 节）：客户端携带的 version 与当前不一致即 409。"""
    if item.version != version:
        raise ApiException(
            409,
            ErrorCodes.WORK_ITEM_VERSION_CONFLICT,
            "任务已被其他成员更新，请刷新后重试",
            details={"current_version": item.version},
        )


async def _get_active_member(session: AsyncSession, member_id: uuid.UUID) -> ProjectMember:
    member = await session.get(ProjectMember, member_id)
    if member is None or not member.is_active:
        raise ApiException(
            422, ErrorCodes.VALIDATION_ERROR, "指定成员不存在或已被禁用", {"member_id": str(member_id)}
        )
    return member


async def _replace_collaborators(
    session: AsyncSession, item: WorkItem, member_ids: list[uuid.UUID]
) -> None:
    unique_ids = list(dict.fromkeys(member_ids))
    for mid in unique_ids:
        await _get_active_member(session, mid)
    # 先清空并 flush：唯一约束 (work_item_id, member_id) 下，同批 flush 会先插后删导致冲突
    item.collaborators = []
    await session.flush()
    item.collaborators = [
        WorkItemCollaborator(work_item_id=item.id, member_id=mid) for mid in unique_ids
    ]
    await session.flush()


def _require_leader(actor: ProjectMember) -> None:
    if actor.role != ROLE_LEADER:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅项目负责人可执行该操作")


# ---------- 用例 ----------


async def create_work_item(
    session: AsyncSession, actor: ProjectMember, payload: WorkItemCreateIn
) -> WorkItemOut:
    """负责人创建工作项（初始 DRAFT），指定主执行人与可选协作者。"""
    _require_leader(actor)
    await _get_active_member(session, payload.assignee_id)

    item = WorkItem(
        title=payload.title,
        description=payload.description,
        acceptance_criteria=payload.acceptance_criteria,
        priority=payload.priority,
        assignee_id=payload.assignee_id,
        due_at=payload.due_at,
    )
    item.collaborators = []  # 显式初始化集合，避免异步懒加载
    session.add(item)
    await session.flush()
    await _replace_collaborators(session, item, payload.collaborator_ids)
    await session.flush()

    await record_event(
        session,
        actor_id=actor.user_id,
        action="work_item.created",
        target_type="work_item",
        target_id=item.id,
        before=None,
        after={
            "title": item.title,
            "status": item.status,
            "priority": item.priority,
            "assignee_id": str(item.assignee_id),
            "collaborator_ids": [str(c.member_id) for c in item.collaborators],
            "due_at": item.due_at.isoformat() if item.due_at else None,
        },
    )
    await session.commit()
    logger.info("work item created: id=%s, assignee_id=%s", item.id, item.assignee_id)
    return await work_item_to_out(session, item)


async def update_work_item(
    session: AsyncSession, actor: ProjectMember, item_id: uuid.UUID, payload: WorkItemUpdateIn
) -> WorkItemOut:
    """负责人修改内容/主执行人/DDL/协作者（乐观锁）。assignee 变化必留痕。"""
    _require_leader(actor)
    item = await get_work_item(session, item_id)
    _check_version(item, payload.version)

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for field in ("title", "description", "acceptance_criteria", "priority", "due_at"):
        new_value = getattr(payload, field)
        if new_value is None:
            continue
        old_value = getattr(item, field)
        if old_value == new_value:
            continue
        before[field] = old_value.isoformat() if isinstance(old_value, datetime) else old_value
        after[field] = new_value.isoformat() if isinstance(new_value, datetime) else new_value
        setattr(item, field, new_value)

    if payload.assignee_id is not None and payload.assignee_id != item.assignee_id:
        await _get_active_member(session, payload.assignee_id)
        before["assignee_id"] = str(item.assignee_id)
        after["assignee_id"] = str(payload.assignee_id)
        item.assignee_id = payload.assignee_id

    if payload.collaborator_ids is not None:
        old_ids = sorted(str(c.member_id) for c in item.collaborators)
        new_ids = sorted(str(mid) for mid in dict.fromkeys(payload.collaborator_ids))
        if old_ids != new_ids:
            await _replace_collaborators(session, item, payload.collaborator_ids)
            before["collaborator_ids"] = old_ids
            after["collaborator_ids"] = new_ids

    if not after:
        return await work_item_to_out(session, item)

    item.version += 1
    await session.flush()
    await record_event(
        session,
        actor_id=actor.user_id,
        action="work_item.updated",
        target_type="work_item",
        target_id=item.id,
        before=before,
        after=after,
    )
    await session.commit()
    await session.refresh(item)  # updated_at 由数据库 onupdate 生成，刷新取回避免异步懒加载
    logger.info("work item updated: id=%s, fields=%s", item.id, sorted(after))
    return await work_item_to_out(session, item)


async def run_command(
    session: AsyncSession, actor: ProjectMember, item_id: uuid.UUID, command: str, version: int
) -> WorkItemOut:
    """状态命令（publish/start/block/unblock/submit/cancel）：状态机 + 乐观锁 + 审计。"""
    item = await get_work_item(session, item_id)

    if _COMMAND_ACTOR[command] == "leader":
        _require_leader(actor)
    elif item.assignee_id != actor.id:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅当前主执行人可执行该操作")
    _check_version(item, version)

    new_status = transition(item.status, command)
    before_status = item.status
    item.status = new_status.value
    item.version += 1
    await session.flush()

    await record_event(
        session,
        actor_id=actor.user_id,
        action=_COMMAND_AUDIT_ACTION[command],
        target_type="work_item",
        target_id=item.id,
        before={"status": before_status},
        after={"status": item.status},
    )
    await session.commit()
    await session.refresh(item)  # updated_at 由数据库 onupdate 生成，刷新取回避免异步懒加载
    logger.info("work item %s: id=%s, %s -> %s", command, item.id, before_status, item.status)
    return await work_item_to_out(session, item)
