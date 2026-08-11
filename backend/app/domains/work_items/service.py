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

# 命令 → 实时事件标题（4.3 节"任务状态变化"；type 复用审计动作名）
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
    """按 id 取工作项（可选项目边界）。

    project_id 传入时做越权 404（spec D3：墙外对象等同不存在）；
    不传则只按 id 取（供派生域经 work_item_id 推导，如转派/交付物）。
    """
    stmt = (
        select(WorkItem)
        .where(WorkItem.id == item_id)
        .options(selectinload(WorkItem.collaborators))  # 预加载，避免异步懒加载
    )
    if for_update:
        # 写路径持行锁（17.2 节）：并发请求在锁后重读，版本检查才能挡下
        # "读取 v1 → 检查通过 → 另一请求已提交" 的交错窗口
        stmt = stmt.with_for_update()
    item = (await session.execute(stmt)).scalar_one_or_none()
    if item is None or (project_id is not None and item.project_id != project_id):
        # 越权 404：项目墙外的工作项与不存在等价，不泄露存在性信息
        raise ApiException(404, ErrorCodes.NOT_FOUND, "工作项不存在")
    return item


async def list_work_items(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    assignee_id: uuid.UUID | None = None,
    status: str | None = None,
    due_from: datetime | None = None,
    due_to: datetime | None = None,
) -> list[WorkItemSummaryOut]:
    """当前项目全量列表（原则 6 透明），支持按负责人、状态、DDL 区间过滤（13.1 节）。"""
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
    """乐观锁（17.2 节）：客户端携带的 version 与当前不一致即 409。"""
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
    """取活跃成员并校验与工作项同项目（spec D3 跨实体引用同项目校验）。

    成员不存在/已禁用 → 422；成员属于其他项目 → 400（跨项目指派被拒绝）。
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


async def _build_status_events(
    session: AsyncSession,
    actor: ProjectMember,
    item: WorkItem,
    command: str,
    before_status: str,
) -> list[OutgoingEvent]:
    """工作项状态变化实时事件（4.3 节，T3.6）。

    接收人按"对端"原则：负责人触发（publish/cancel）→ 主执行人；
    主执行人触发（start/block/unblock/submit）→ 全体活跃负责人；
    触发者本人不重复接收（其 REST 响应已确认结果）。
    只发 SSE 事件、不写站内通知——状态变化在列表/看板可见，不属于待办。
    """
    if _COMMAND_ACTOR[command] == "assignee":
        # 通知该项目（工作项所属项目）的全体活跃负责人（spec 4.3 节）
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
    # spec D3：assignee 与协作者必须与工作项同项目（跨项目指派 → 400）
    await _get_active_member(session, payload.assignee_id, project_id=actor.project_id)

    item = WorkItem(
        # 项目归属从请求上下文填充（spec D3：API 不接受传入，service 层派生）
        project_id=actor.project_id,
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
    await session.refresh(item)  # updated_at 由数据库 onupdate 生成，刷新取回避免异步懒加载
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
        # 提交审核前必须已存在交付物（7.5 节，T4.4）：无交付物拒绝并提示先提交
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
        # 开发文档前置（设计文档 2026-07-30 §4.3）：存在 CONFIRMED 文档或已豁免
        # 才允许开工；unblock（BLOCKED → IN_PROGRESS）不重复校验；
        # 校验放在状态机裁决之后，非法迁移（如 DRAFT 直接 start）优先报迁移错误
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
    # commit 成功后发布实时事件（4.3 节）：订阅者收到的必为已落库事实
    await publish_after_commit(await _build_status_events(session, actor, item, command, before_status))
    if command == "submit":
        # 提交审核后触发交付物初审 Agent（T5.5，event 触发）：业务事务已 commit
        # 再投递；尽力而为，投递失败不影响已完成的 submit（17.3 节）
        await _dispatch_deliverable_review(session, item)
    await session.refresh(item)  # updated_at 由数据库 onupdate 生成，刷新取回避免异步懒加载
    logger.info("work item %s: id=%s, %s -> %s", command, item.id, before_status, item.status)
    return await work_item_to_out(session, item)


async def _dispatch_deliverable_review(session: AsyncSession, item: WorkItem) -> None:
    """投递 deliverable_review 的 agent.run（trigger_source="event"），失败只记日志。"""
    redis_client = create_redis_client()
    try:
        run = await request_agent_analysis(
            session,
            redis_client,
            agent_type=DELIVERABLE_REVIEW_AGENT_TYPE,
            trigger_source="event",
            work_item_id=item.id,
        )
        logger.info(
            "deliverable review dispatched: run_id=%s work_item_id=%s", run.id, item.id
        )
    except Exception:  # noqa: BLE001 - Agent 投递失败不拖垮已提交的 submit（17.3 节）
        logger.warning(
            "deliverable review dispatch failed, submit unaffected: work_item_id=%s", item.id
        )
    finally:
        await redis_client.aclose()
