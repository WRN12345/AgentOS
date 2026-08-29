"""工作项应用服务与权限策略。

权限规则由各用例显式校验：
- 创建、修改、发布、取消：仅项目负责人；
- start / block / unblock / submit：仅当前主执行人；
- 查询：任何项目成员。
状态迁移由 domains/work_items/state_machine.py 裁决；状态或字段变更与审计事件
在同一事务写入，assignee 变化必须留痕。
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import ApiException, ErrorCodes
from app.core.logging import setup_logging
from app.agents.service import request_agent_analysis
from app.agents.specialists.review import AGENT_TYPE as DELIVERABLE_REVIEW_AGENT_TYPE
from app.domains.audit.service import record_event
from app.domains.deliverables.models import Deliverable
from app.domains.dev_docs.models import DevDoc
from app.domains.dev_docs.state_machine import DevDocStatus
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
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.events import OutgoingEvent, publish_after_commit

logger = setup_logging("backend")

_COMMAND_AUDIT_ACTION = {
    "publish": "work_item.published",
    "start": "work_item.started",
    "block": "work_item.blocked",
    "unblock": "work_item.unblocked",
    "submit": "work_item.submitted",
    "cancel": "work_item.cancelled",
}

_COMMAND_ACTOR = {
    "publish": "leader",
    "start": "assignee",
    "block": "assignee",
    "unblock": "assignee",
    "submit": "assignee",
    "cancel": "leader",
}

_COMMAND_EVENT_TITLE = {
    "publish": "工作项已发布",
    "start": "工作项已开始",
    "block": "工作项被阻塞",
    "unblock": "工作项已解除阻塞",
    "submit": "工作项已提交审核",
    "cancel": "工作项已取消",
}


# ---------- 查询 ----------


async def get_work_item(
    session: AsyncSession,
    item_id: uuid.UUID,
    *,
    for_update: bool = False,
    project_id: uuid.UUID | None = None,
) -> WorkItem:
    """按 id 获取工作项，可选校验项目边界。

    传入 project_id 时，跨项目对象按不存在处理；不传则供派生域通过
    work_item_id 查询父对象。
    """
    stmt = (
        select(WorkItem)
        .where(WorkItem.id == item_id)
        .options(selectinload(WorkItem.collaborators))  # 预加载，避免异步懒加载
    )
    if for_update:
        # 行锁让并发写请求基于最新已提交数据执行版本检查
        stmt = stmt.with_for_update()
    item = (await session.execute(stmt)).scalar_one_or_none()
    if item is None or (project_id is not None and item.project_id != project_id):
        # 跨项目对象按不存在处理，避免泄露其存在性
        raise ApiException(404, ErrorCodes.NOT_FOUND, "工作项不存在")
    return item


async def get_work_item_project_id(
    session: AsyncSession, work_item_id: uuid.UUID
) -> uuid.UUID | None:
    """获取工作项的项目归属，供派生域从父对象确定项目边界。

    派生表不冗余 project_id；工作项不存在时返回 None，由调用方按 404 处理。
    """
    return (
        await session.execute(
            select(WorkItem.project_id).where(WorkItem.id == work_item_id)
        )
    ).scalar_one_or_none()


async def list_work_items(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    assignee_id: uuid.UUID | None = None,
    status: str | None = None,
    due_from: datetime | None = None,
    due_to: datetime | None = None,
) -> list[WorkItemSummaryOut]:
    """返回当前项目工作项，支持按负责人、状态和 DDL 区间过滤。"""
    stmt = select(WorkItem).where(WorkItem.project_id == project_id)
    stmt = stmt.order_by(WorkItem.created_at.desc())
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
    """校验乐观锁版本，不一致时返回 409。"""
    if item.version != version:
        raise ApiException(
            409,
            ErrorCodes.WORK_ITEM_VERSION_CONFLICT,
            "任务已被其他成员更新，请刷新后重试",
            details={"current_version": item.version},
        )


async def _get_active_member(
    session: AsyncSession, member_id: uuid.UUID, *, project_id: uuid.UUID
) -> ProjectMember:
    """获取活跃成员并校验项目边界。

    成员不存在或已禁用时返回 422，跨项目指派时返回 400。
    """
    member = await session.get(ProjectMember, member_id)
    if member is None or not member.is_active:
        raise ApiException(
            422, ErrorCodes.VALIDATION_ERROR, "指定成员不存在或已被禁用", {"member_id": str(member_id)}
        )
    if member.project_id != project_id:
        raise ApiException(
            400,
            ErrorCodes.CROSS_PROJECT_REFERENCE,
            "不能指派其他项目的成员",
            {"member_id": str(member_id)},
        )
    return member


async def _replace_collaborators(
    session: AsyncSession, item: WorkItem, member_ids: list[uuid.UUID], *, project_id: uuid.UUID
) -> None:
    unique_ids = list(dict.fromkeys(member_ids))
    for mid in unique_ids:
        await _get_active_member(session, mid, project_id=project_id)
    # 先删除再 flush，避免 ORM 在唯一约束下先插入新关联而产生冲突
    item.collaborators = []
    await session.flush()
    item.collaborators = [
        WorkItemCollaborator(work_item_id=item.id, member_id=mid) for mid in unique_ids
    ]
    await session.flush()


def _require_leader(actor: ProjectMember) -> None:
    if actor.role != ROLE_LEADER:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅项目负责人可执行该操作")


async def _build_status_events(
    session: AsyncSession,
    actor: ProjectMember,
    item: WorkItem,
    command: str,
    before_status: str,
) -> list[OutgoingEvent]:
    """构建工作项状态变化的实时事件。

    负责人触发时发送给主执行人，主执行人触发时发送给全体活跃负责人。
    触发者已通过 REST 响应确认结果，不重复接收。状态变化仅发布 SSE，
    不创建站内待办通知。
    """
    if _COMMAND_ACTOR[command] == "assignee":
        # 仅发送给工作项所属项目的活跃负责人
        leaders = (
            (
                await session.execute(
                    select(ProjectMember).where(
                        ProjectMember.project_id == item.project_id,
                        ProjectMember.role == ROLE_LEADER,
                        ProjectMember.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        recipient_ids = [leader.id for leader in leaders]
    else:
        recipient_ids = [item.assignee_id]

    event_type = _COMMAND_AUDIT_ACTION[command]
    return [
        OutgoingEvent(
            project_id=item.project_id,
            recipient_id=recipient_id,
            type=event_type,
            title=_COMMAND_EVENT_TITLE[command],
            body=(
                f"{actor.display_name} 将工作项「{item.title}」"
                f"状态从 {before_status} 变更为 {item.status}"
            ),
            link=f"/work-items/{item.id}",
        )
        for recipient_id in dict.fromkeys(recipient_ids)
        if recipient_id != actor.id
    ]


# ---------- 用例 ----------


async def create_work_item(
    session: AsyncSession, actor: ProjectMember, payload: WorkItemCreateIn
) -> WorkItemOut:
    """负责人创建工作项（初始 DRAFT），指定主执行人与可选协作者。"""
    _require_leader(actor)
    # 拒绝跨项目指派，避免建立跨项目成员引用
    await _get_active_member(session, payload.assignee_id, project_id=actor.project_id)

    item = WorkItem(
        # 项目归属取自认证上下文，防止客户端指定其他项目
        project_id=actor.project_id,
        title=payload.title,
        description=payload.description,
        acceptance_criteria=payload.acceptance_criteria,
        priority=payload.priority,
        assignee_id=payload.assignee_id,
        due_at=payload.due_at,
    )
    item.collaborators = []  # 避免首次写入触发异步懒加载
    session.add(item)
    await session.flush()
    await _replace_collaborators(
        session, item, payload.collaborator_ids, project_id=actor.project_id
    )
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
    item = await get_work_item(session, item_id, for_update=True, project_id=actor.project_id)
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
        await _get_active_member(session, payload.assignee_id, project_id=actor.project_id)
        before["assignee_id"] = str(item.assignee_id)
        after["assignee_id"] = str(payload.assignee_id)
        item.assignee_id = payload.assignee_id

    if payload.collaborator_ids is not None:
        old_ids = sorted(str(c.member_id) for c in item.collaborators)
        new_ids = sorted(str(mid) for mid in dict.fromkeys(payload.collaborator_ids))
        if old_ids != new_ids:
            await _replace_collaborators(
                session, item, payload.collaborator_ids, project_id=actor.project_id
            )
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
    await session.refresh(item)  # 取回数据库生成的 updated_at，避免后续异步懒加载
    logger.info("work item updated: id=%s, fields=%s", item.id, sorted(after))
    return await work_item_to_out(session, item)


async def run_command(
    session: AsyncSession, actor: ProjectMember, item_id: uuid.UUID, command: str, version: int
) -> WorkItemOut:
    """状态命令（publish/start/block/unblock/submit/cancel）：状态机 + 乐观锁 + 审计。"""
    item = await get_work_item(session, item_id, for_update=True, project_id=actor.project_id)

    if _COMMAND_ACTOR[command] == "leader":
        _require_leader(actor)
    elif item.assignee_id != actor.id:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅当前主执行人可执行该操作")
    _check_version(item, version)

    if command == "submit":
        # 没有交付物时禁止进入审核，避免产生无审核对象的 IN_REVIEW 工作项
        deliverable_exists = (
            await session.execute(
                select(Deliverable.id).where(Deliverable.work_item_id == item.id).limit(1)
            )
        ).scalar_one_or_none()
        if deliverable_exists is None:
            raise ApiException(
                422,
                ErrorCodes.DELIVERABLE_REQUIRED,
                "提交审核前请先提交交付物",
            )

    new_status = transition(item.status, command)

    if command == "start":
        # 开工需要开发文档已确认或已豁免；unblock 不重复校验。
        # 先裁决状态机，确保非法迁移优先返回迁移错误
        doc = (
            await session.execute(
                select(DevDoc).where(DevDoc.work_item_id == item.id).limit(1)
            )
        ).scalar_one_or_none()
        confirmed = doc is not None and doc.status == DevDocStatus.CONFIRMED.value
        waived = doc is not None and doc.waived
        if not (confirmed or waived):
            raise ApiException(
                409,
                ErrorCodes.DEV_DOC_REQUIRED,
                "请先提交开发文档并通过负责人确认",
            )

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
    # commit 后再发布，确保订阅者收到的状态已经落库
    await publish_after_commit(await _build_status_events(session, actor, item, command, before_status))
    if command == "submit":
        # 初审 Agent 在事务提交后以 best-effort 方式投递，失败不回滚 submit
        await _dispatch_deliverable_review(session, item)
    await session.refresh(item)  # 取回数据库生成的 updated_at，避免后续异步懒加载
    logger.info("work item %s: id=%s, %s -> %s", command, item.id, before_status, item.status)
    return await work_item_to_out(session, item)


async def _dispatch_deliverable_review(session: AsyncSession, item: WorkItem) -> None:
    """通过 event 投递 deliverable_review agent.run，失败仅记录日志。"""
    redis_client = create_redis_client()
    try:
        run = await request_agent_analysis(
            session,
            redis_client,
            agent_type=DELIVERABLE_REVIEW_AGENT_TYPE,
            project_id=item.project_id,
            trigger_source="event",
            work_item_id=item.id,
        )
        logger.info(
            "deliverable review dispatched: run_id=%s work_item_id=%s", run.id, item.id
        )
    except Exception:  # noqa: BLE001 - Agent 投递失败不能影响已完成的提交
        logger.warning(
            "deliverable review dispatch failed, submit unaffected: work_item_id=%s", item.id
        )
    finally:
        await redis_client.aclose()
