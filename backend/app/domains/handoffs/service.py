import uuid
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiException, ErrorCodes
from app.core.logging import setup_logging
from app.domains.audit.service import record_event
from app.domains.deliverables.models import Deliverable
from app.domains.deliverables.service import _to_out as deliverable_to_out
from app.domains.files.service import is_work_item_related
from app.domains.handoffs.models import DeliverableHandoff
from app.domains.handoffs.policy import ensure_no_pending_handoff
from app.domains.handoffs.schemas import (
    HandoffCreateIn,
    HandoffOut,
    HandoffResponseIn,
    HandoffTargetOut,
    HandoffWorkItemBrief,
)
from app.domains.memory.history import enqueue_work_item_conclusion_index
from app.domains.memory.summary import enqueue_work_item_summary
from app.domains.notifications.service import notify
from app.domains.project.models import ROLE_LEADER, ProjectMember
from app.domains.work_items.models import WorkItem
from app.domains.work_items.schemas import MemberBrief
from app.domains.work_items.service import _dispatch_deliverable_review
from app.domains.work_items.state_machine import transition
from app.infrastructure.events import OutgoingEvent, publish_after_commit

logger = setup_logging("backend")


async def _item(
    session: AsyncSession, actor: ProjectMember, item_id: uuid.UUID
) -> WorkItem:
    item = await session.get(WorkItem, item_id)
    if item is None or item.project_id != actor.project_id:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "工作项不存在")
    return item


async def _lock_items(
    session: AsyncSession,
    actor: ProjectMember,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
) -> tuple[WorkItem, WorkItem]:
    # 所有修改操作先按 UUID 顺序锁定两端工作项，再锁定移交记录。
    items = (
        await session.scalars(
            select(WorkItem)
            .where(
                WorkItem.id.in_({source_id, target_id}),
                WorkItem.project_id == actor.project_id,
            )
            .order_by(WorkItem.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    by_id = {item.id: item for item in items}
    if source_id not in by_id or target_id not in by_id:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "工作项不存在")
    return by_id[source_id], by_id[target_id]


def _version(current: int, expected: int) -> None:
    if current != expected:
        raise ApiException(
            409,
            "HANDOFF_VERSION_CONFLICT",
            "版本已更新，请刷新后重试",
            {"current_version": current},
        )


async def _to_out(session: AsyncSession, handoff: DeliverableHandoff) -> HandoffOut:
    source = await session.get(WorkItem, handoff.source_work_item_id)
    target = await session.get(WorkItem, handoff.target_work_item_id)
    delivery = await session.get(Deliverable, handoff.deliverable_id)
    sender = await session.get(ProjectMember, handoff.sender_id)
    recipient = await session.get(ProjectMember, handoff.recipient_id)
    return HandoffOut(
        id=handoff.id,
        source_work_item=HandoffWorkItemBrief.model_validate(source),
        target_work_item=HandoffWorkItemBrief.model_validate(target),
        deliverable=await deliverable_to_out(session, delivery),
        sender=MemberBrief(id=sender.id, display_name=sender.display_name),
        recipient=MemberBrief(id=recipient.id, display_name=recipient.display_name),
        status=handoff.status,
        note=handoff.note,
        response_note=handoff.response_note,
        version=handoff.version,
        created_at=handoff.created_at,
        updated_at=handoff.updated_at,
        responded_at=handoff.responded_at,
    )


async def _visible(
    session: AsyncSession, actor: ProjectMember, handoff: DeliverableHandoff
) -> bool:
    if actor.role == ROLE_LEADER or actor.id in (
        handoff.sender_id,
        handoff.recipient_id,
    ):
        return True
    target = await session.get(WorkItem, handoff.target_work_item_id)
    if target.assignee_id == actor.id:
        return True
    if handoff.status == "accepted":
        return await is_work_item_related(
            session, handoff.target_work_item_id, actor.id
        ) or await is_work_item_related(session, handoff.source_work_item_id, actor.id)
    return False


async def list_targets(
    session: AsyncSession,
    actor: ProjectMember,
    source_work_item_id: uuid.UUID,
) -> list[HandoffTargetOut]:
    source = await _item(session, actor, source_work_item_id)
    if actor.role != ROLE_LEADER and source.assignee_id != actor.id:
        raise ApiException(
            403, ErrorCodes.FORBIDDEN, "仅主执行人或负责人可查看移交目标"
        )
    rows = (
        await session.execute(
            select(WorkItem, ProjectMember)
            .join(ProjectMember, ProjectMember.id == WorkItem.assignee_id)
            .where(
                WorkItem.project_id == actor.project_id,
                WorkItem.status == "READY",
                WorkItem.id != source.id,
                WorkItem.assignee_id != source.assignee_id,
                ProjectMember.project_id == actor.project_id,
                ProjectMember.is_active.is_(True),
            )
            .order_by(WorkItem.title, WorkItem.id)
        )
    ).all()
    return [
        HandoffTargetOut(
            **HandoffWorkItemBrief.model_validate(item).model_dump(),
            assignee=MemberBrief(id=member.id, display_name=member.display_name),
        )
        for item, member in rows
    ]


async def list_for_work_item(
    session: AsyncSession,
    actor: ProjectMember,
    item_id: uuid.UUID,
) -> list[HandoffOut]:
    await _item(session, actor, item_id)
    handoffs = (
        await session.scalars(
            select(DeliverableHandoff)
            .where(
                DeliverableHandoff.project_id == actor.project_id,
                or_(
                    DeliverableHandoff.source_work_item_id == item_id,
                    DeliverableHandoff.target_work_item_id == item_id,
                ),
            )
            .order_by(DeliverableHandoff.created_at.desc())
        )
    ).all()
    visible = [h for h in handoffs if await _visible(session, actor, h)]
    if (
        not visible
        and actor.role != ROLE_LEADER
        and not await is_work_item_related(session, item_id, actor.id)
    ):
        raise ApiException(403, ErrorCodes.FORBIDDEN, "无权查看该工作项的移交")
    return [await _to_out(session, h) for h in visible]


async def list_inbox(
    session: AsyncSession,
    actor: ProjectMember,
    role: str,
    status: str | None,
) -> list[HandoffOut]:
    stmt = select(DeliverableHandoff).where(
        DeliverableHandoff.project_id == actor.project_id
    )
    column = (
        DeliverableHandoff.recipient_id
        if role == "received"
        else DeliverableHandoff.sender_id
    )
    stmt = stmt.where(column == actor.id)
    if status is not None:
        stmt = stmt.where(DeliverableHandoff.status == status)
    handoffs = (
        await session.scalars(stmt.order_by(DeliverableHandoff.created_at.desc()))
    ).all()
    return [await _to_out(session, h) for h in handoffs]


async def _record(
    session: AsyncSession,
    actor: ProjectMember,
    handoff: DeliverableHandoff,
    source: WorkItem,
    before_status: str,
    action: str,
) -> list[OutgoingEvent]:
    events: list[OutgoingEvent] = []
    await record_event(
        session,
        actor_id=actor.user_id,
        action=f"handoff.{action}",
        target_type="deliverable_handoff",
        target_id=handoff.id,
        before=None if action == "created" else {"status": "pending"},
        after={
            "status": handoff.status,
            "source_work_item_id": str(source.id),
            "target_work_item_id": str(handoff.target_work_item_id),
            "deliverable_id": str(handoff.deliverable_id),
        },
    )
    await record_event(
        session,
        actor_id=actor.user_id,
        action="work_item.status_changed",
        target_type="work_item",
        target_id=source.id,
        before={"status": before_status},
        after={"status": source.status, "handoff_id": str(handoff.id)},
    )
    titles = {
        "created": "成果待接收",
        "accepted": "成果已接收",
        "changes_requested": "成果需要补充",
    }
    for member_id in (handoff.sender_id, handoff.recipient_id):
        await notify(
            session,
            project_id=handoff.project_id,
            recipient_id=member_id,
            type=f"handoff.{action}",
            title=titles[action],
            body=f"工作项「{source.title}」{titles[action]}",
            link=f"/work-items/{source.id if member_id == handoff.sender_id else handoff.target_work_item_id}",
            outbox=events,
        )
    return events


async def create_handoff(
    session: AsyncSession,
    actor: ProjectMember,
    item_id: uuid.UUID,
    payload: HandoffCreateIn,
) -> HandoffOut:
    source, target = await _lock_items(
        session, actor, item_id, payload.target_work_item_id
    )
    if not actor.is_active or source.assignee_id != actor.id:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅当前主执行人可移交成果")
    _version(source.version, payload.version)
    _version(target.version, payload.target_version)
    if source.id == target.id or source.assignee_id == target.assignee_id:
        raise ApiException(422, ErrorCodes.VALIDATION_ERROR, "请选择其他成员的工作项")
    if source.status != "IN_PROGRESS" or target.status != "READY":
        raise ApiException(
            409, "HANDOFF_INVALID_STATE", "移交需要执行中的来源和待开始的目标"
        )
    recipient = await session.get(ProjectMember, target.assignee_id)
    if (
        recipient is None
        or not recipient.is_active
        or recipient.project_id != actor.project_id
    ):
        raise ApiException(422, ErrorCodes.VALIDATION_ERROR, "接收人必须是项目活跃成员")
    await ensure_no_pending_handoff(session, source.id)
    latest = await session.scalar(
        select(Deliverable)
        .where(
            Deliverable.work_item_id == source.id,
            Deliverable.project_id == actor.project_id,
        )
        .order_by(Deliverable.version.desc())
        .limit(1)
    )
    if latest is None or latest.id != payload.deliverable_id:
        raise ApiException(
            409, "HANDOFF_DELIVERABLE_CONFLICT", "请选择来源工作项的最新交付物"
        )
    handoff = DeliverableHandoff(
        project_id=actor.project_id,
        source_work_item_id=source.id,
        target_work_item_id=target.id,
        deliverable_id=latest.id,
        sender_id=actor.id,
        recipient_id=recipient.id,
        note=payload.note,
        status="pending",
    )
    before_status = source.status
    source.status = transition(source.status, "handoff").value
    source.version += 1
    session.add(handoff)
    await session.flush()
    events = await _record(session, actor, handoff, source, before_status, "created")
    await session.commit()
    await publish_after_commit(events)
    await session.refresh(handoff)
    await session.refresh(source)
    result = await _to_out(session, handoff)
    try:
        await _dispatch_deliverable_review(session, source)
    except Exception:  # noqa: BLE001 - 派生任务不能回滚已提交的移交。
        logger.warning("Handoff review dispatch failed: handoff_id=%s", result.id)
    return result


async def respond(
    session: AsyncSession,
    actor: ProjectMember,
    handoff_id: uuid.UUID,
    payload: HandoffResponseIn,
    *,
    accept: bool,
) -> HandoffOut:
    handoff = await session.get(DeliverableHandoff, handoff_id)
    if handoff is None or handoff.project_id != actor.project_id:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "移交不存在")
    source, target = await _lock_items(
        session, actor, handoff.source_work_item_id, handoff.target_work_item_id
    )
    handoff = await session.scalar(
        select(DeliverableHandoff)
        .where(
            DeliverableHandoff.id == handoff_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        not actor.is_active
        or actor.id != handoff.recipient_id
        or target.assignee_id != actor.id
    ):
        raise ApiException(
            403, ErrorCodes.FORBIDDEN, "仅指定接收人且仍为目标主执行人可响应"
        )
    _version(handoff.version, payload.version)
    if (
        handoff.status != "pending"
        or source.status != "WAITING_ACCEPTANCE"
        or target.status != "READY"
        or source.assignee_id != handoff.sender_id
    ):
        raise ApiException(409, "HANDOFF_INVALID_STATE", "移交状态已变化，请刷新后重试")
    if not accept and not (payload.note and payload.note.strip()):
        raise ApiException(422, ErrorCodes.VALIDATION_ERROR, "要求补充时请填写说明")
    before_status = source.status
    source.status = transition(
        source.status, "accept_handoff" if accept else "return_handoff"
    ).value
    source.version += 1
    handoff.status = "accepted" if accept else "changes_requested"
    handoff.response_note = payload.note.strip() if payload.note else None
    handoff.responded_at = datetime.now(UTC)
    handoff.version += 1
    await session.flush()
    events = await _record(
        session, actor, handoff, source, before_status, handoff.status
    )
    await session.commit()
    await publish_after_commit(events)
    await session.refresh(handoff)
    await session.refresh(source)
    result = await _to_out(session, handoff)
    if accept:
        for enqueue in (enqueue_work_item_conclusion_index, enqueue_work_item_summary):
            try:
                await enqueue(source)
            except Exception:  # noqa: BLE001 - 各派生任务独立尽力执行。
                logger.warning(
                    "Handoff conclusion dispatch failed: handoff_id=%s", result.id
                )
    return result
