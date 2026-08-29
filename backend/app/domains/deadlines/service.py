"""DDL 变更申请应用服务与权限策略。

每个用例显式校验权限：主任务级申请仅当前主执行人或项目负责人可发起；
协作级申请仅协作双方可发起；`approve` 和 `reject` 仅项目负责人可执行；
`cancel` 仅申请人可执行。

协作级新 DDL 不晚于主任务 DDL，或主任务没有 DDL 时，可由双方直接确认生效：
同一事务更新 `collaboration_requests.due_at` 并记录为 `APPROVED`。
- 影响主任务 DDL 的协作级变更与一切主任务级变更走负责人审批流；
- 同一工作项只能有一个待审批主 DDL 变更：应用层先查重，数据库唯一部分索引
  负责封闭并发窗口；
- 规则化影响分析在创建时同步生成并推进到 PENDING_APPROVAL；分析异常时
  `impact_analysis_status=unavailable` 后照常推进，不阻塞人工审批。
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiException, ErrorCodes
from app.core.logging import setup_logging
from app.domains.audit.service import record_event
from app.domains.collaboration.models import CollaborationRequest
from app.domains.collaboration.state_machine import CollaborationStatus
from app.domains.deadlines.models import DeadlineChangeRequest
from app.domains.deadlines.schemas import (
    DeadlineChangeCreateIn,
    DeadlineChangeRequestOut,
    DeadlineChangeSummaryOut,
)
from app.domains.deadlines.state_machine import (
    DeadlineChangeStatus,
    DeadlineTargetType,
    ImpactAnalysisStatus,
    transition,
)
from app.domains.notifications.service import notify
from app.domains.project.models import ROLE_LEADER, ProjectMember
from app.domains.work_items.models import WorkItem
from app.domains.work_items.schemas import MemberBrief
from app.domains.work_items.service import get_work_item, get_work_item_project_id
from app.infrastructure.events import OutgoingEvent, publish_after_commit

logger = setup_logging("backend")

# 协作请求进入终态后不再允许变更 DDL。
_COLLAB_TERMINAL = (
    CollaborationStatus.DECLINED.value,
    CollaborationStatus.CANCELLED.value,
    CollaborationStatus.COMPLETED.value,
)




def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


async def generate_impact_analysis(
    session: AsyncSession,
    *,
    target_type: str,
    target_id: uuid.UUID,
    old_due_at: datetime | None,
    new_due_at: datetime,
    work_item: WorkItem,
) -> dict[str, Any]:
    """创建申请时在同一事务内同步生成规则化影响分析。

    内容：目标新旧 DDL 对比、是否晚于主任务 DDL、该工作项下未完成协作请求
    及其 DDL。分析失败由调用方捕获并标记为 `unavailable`，不阻塞人工审批。
    """
    open_collabs = list(
        (
            await session.execute(
                select(CollaborationRequest)
                .where(
                    CollaborationRequest.work_item_id == work_item.id,
                    CollaborationRequest.status.notin_(_COLLAB_TERMINAL),
                )
                .order_by(CollaborationRequest.created_at)
            )
        )
        .scalars()
        .all()
    )
    exceeds_work_item_due = work_item.due_at is not None and new_due_at > work_item.due_at
    return {
        "target": {
            "type": target_type,
            "id": str(target_id),
            "old_due_at": _iso(old_due_at),
            "new_due_at": _iso(new_due_at),
        },
        "work_item": {
            "id": str(work_item.id),
            "title": work_item.title,
            "due_at": _iso(work_item.due_at),
        },
        "exceeds_work_item_due": exceeds_work_item_due,
        "affected_collaboration_requests": [
            {
                "id": str(c.id),
                "title": c.title,
                "status": c.status,
                "requester_id": str(c.requester_id),
                "assignee_id": str(c.assignee_id),
                "due_at": _iso(c.due_at),
            }
            for c in open_collabs
        ],
    }




async def get_request(
    session: AsyncSession,
    request_id: uuid.UUID,
    *,
    for_update: bool = False,
    project_id: uuid.UUID | None = None,
) -> DeadlineChangeRequest:
    # 写路径通过 `for_update=True` 持有行锁，使并发审批串行读取最新状态。
    request = await session.get(DeadlineChangeRequest, request_id, with_for_update=for_update)
    if request is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "DDL 变更申请不存在")
    if project_id is not None and (
        await get_work_item_project_id(session, request.work_item_id) != project_id
    ):
        # 将跨项目访问视为资源不存在，避免泄露资源存在性。
        raise ApiException(404, ErrorCodes.NOT_FOUND, "DDL 变更申请不存在")
    return request


async def _target_titles(
    session: AsyncSession, requests: list[DeadlineChangeRequest]
) -> dict[uuid.UUID, str]:
    """按目标类型批量加载工作项或协作请求标题。"""
    titles: dict[uuid.UUID, str] = {}
    item_ids = {r.target_id for r in requests if r.target_type == DeadlineTargetType.WORK_ITEM}
    collab_ids = {
        r.target_id for r in requests if r.target_type == DeadlineTargetType.COLLABORATION_REQUEST
    }
    if item_ids:
        rows = await session.execute(select(WorkItem.id, WorkItem.title).where(WorkItem.id.in_(item_ids)))
        titles.update({row.id: row.title for row in rows})
    if collab_ids:
        rows = await session.execute(
            select(CollaborationRequest.id, CollaborationRequest.title).where(
                CollaborationRequest.id.in_(collab_ids)
            )
        )
        titles.update({row.id: row.title for row in rows})
    return titles


async def _load_context(
    session: AsyncSession, requests: list[DeadlineChangeRequest]
) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, str], dict[uuid.UUID, MemberBrief]]:
    """批量取工作项标题、目标标题与相关成员显示名。"""
    item_ids = {r.work_item_id for r in requests}
    member_ids: set[uuid.UUID] = {r.requested_by for r in requests}
    member_ids.update(r.approved_by for r in requests if r.approved_by is not None)

    item_titles: dict[uuid.UUID, str] = {}
    if item_ids:
        rows = await session.execute(
            select(WorkItem.id, WorkItem.title).where(WorkItem.id.in_(item_ids))
        )
        item_titles = {row.id: row.title for row in rows}

    briefs: dict[uuid.UUID, MemberBrief] = {}
    if member_ids:
        members = (
            (await session.execute(select(ProjectMember).where(ProjectMember.id.in_(member_ids))))
            .scalars()
            .all()
        )
        briefs = {m.id: MemberBrief(id=m.id, display_name=m.display_name) for m in members}
    return item_titles, await _target_titles(session, requests), briefs


def _brief(briefs: dict[uuid.UUID, MemberBrief], member_id: uuid.UUID | None) -> MemberBrief | None:
    if member_id is None:
        return None
    return briefs.get(member_id) or MemberBrief(id=member_id, display_name="")


def _to_out(
    request: DeadlineChangeRequest,
    item_titles: dict[uuid.UUID, str],
    target_titles: dict[uuid.UUID, str],
    briefs: dict[uuid.UUID, MemberBrief],
) -> DeadlineChangeRequestOut:
    return DeadlineChangeRequestOut(
        id=request.id,
        work_item_id=request.work_item_id,
        work_item_title=item_titles.get(request.work_item_id, ""),
        target_type=request.target_type,
        target_id=request.target_id,
        target_title=target_titles.get(request.target_id, ""),
        old_due_at=request.old_due_at,
        new_due_at=request.new_due_at,
        reason=request.reason,
        impact_analysis=request.impact_analysis,
        impact_analysis_status=request.impact_analysis_status,
        status=request.status,
        requested_by=_brief(briefs, request.requested_by),  # type: ignore[arg-type]
        approved_by=_brief(briefs, request.approved_by),
        approved_at=request.approved_at,
        version=request.version,
        created_at=request.created_at,
        updated_at=request.updated_at,
    )


def _to_summary(
    request: DeadlineChangeRequest,
    item_titles: dict[uuid.UUID, str],
    target_titles: dict[uuid.UUID, str],
    briefs: dict[uuid.UUID, MemberBrief],
) -> DeadlineChangeSummaryOut:
    return DeadlineChangeSummaryOut(
        id=request.id,
        work_item_id=request.work_item_id,
        work_item_title=item_titles.get(request.work_item_id, ""),
        target_type=request.target_type,
        target_id=request.target_id,
        target_title=target_titles.get(request.target_id, ""),
        old_due_at=request.old_due_at,
        new_due_at=request.new_due_at,
        impact_analysis_status=request.impact_analysis_status,
        status=request.status,
        requested_by=_brief(briefs, request.requested_by),  # type: ignore[arg-type]
        version=request.version,
        created_at=request.created_at,
        updated_at=request.updated_at,
    )


async def request_to_out(
    session: AsyncSession, request: DeadlineChangeRequest
) -> DeadlineChangeRequestOut:
    item_titles, target_titles, briefs = await _load_context(session, [request])
    return _to_out(request, item_titles, target_titles, briefs)


async def get_detail(
    session: AsyncSession, request_id: uuid.UUID, *, project_id: uuid.UUID | None = None
) -> DeadlineChangeRequestOut:
    """返回项目成员可见的详情，包括 `reason` 和 `impact_analysis`。"""
    request = await get_request(session, request_id, project_id=project_id)
    return await request_to_out(session, request)


async def list_for_work_item(
    session: AsyncSession, item_id: uuid.UUID, *, project_id: uuid.UUID | None = None
) -> list[DeadlineChangeSummaryOut]:
    """返回项目成员可见的工作项 DDL 变更历史。"""
    await get_work_item(session, item_id, project_id=project_id)  # 越权 → 404
    requests = list(
        (
            await session.execute(
                select(DeadlineChangeRequest)
                .where(DeadlineChangeRequest.work_item_id == item_id)
                .order_by(DeadlineChangeRequest.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    item_titles, target_titles, briefs = await _load_context(session, requests)
    return [_to_summary(r, item_titles, target_titles, briefs) for r in requests]


async def list_mine(
    session: AsyncSession, actor: ProjectMember
) -> list[DeadlineChangeSummaryOut]:
    """返回当前项目中本人发起的 DDL 变更申请。"""
    requests = list(
        (
            await session.execute(
                select(DeadlineChangeRequest)
                .join(WorkItem, WorkItem.id == DeadlineChangeRequest.work_item_id)
                .where(
                    DeadlineChangeRequest.requested_by == actor.id,
                    WorkItem.project_id == actor.project_id,
                )
                .order_by(DeadlineChangeRequest.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    item_titles, target_titles, briefs = await _load_context(session, requests)
    return [_to_summary(r, item_titles, target_titles, briefs) for r in requests]


async def list_pending_approval(
    session: AsyncSession, *, project_id: uuid.UUID | None = None
) -> list[DeadlineChangeRequest]:
    """返回当前项目中状态为 `PENDING_APPROVAL` 的申请。"""
    stmt = (
        select(DeadlineChangeRequest)
        .join(WorkItem, WorkItem.id == DeadlineChangeRequest.work_item_id)
        .where(DeadlineChangeRequest.status == DeadlineChangeStatus.PENDING_APPROVAL.value)
        .order_by(DeadlineChangeRequest.created_at.desc())
    )
    if project_id is not None:
        stmt = stmt.where(WorkItem.project_id == project_id)
    return list((await session.execute(stmt)).scalars().all())




def _check_version(request: DeadlineChangeRequest, version: int) -> None:
    """校验乐观锁版本，不一致时返回 `409`。"""
    if request.version != version:
        raise ApiException(
            409,
            ErrorCodes.DEADLINE_CHANGE_VERSION_CONFLICT,
            "DDL 变更申请已被其他成员更新，请刷新后重试",
            details={"current_version": request.version},
        )


async def _notify_leaders(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    type: str,
    title: str,
    body: str,
    link: str,
    outbox: list[OutgoingEvent] | None = None,
) -> None:
    """通知指定项目的全体活跃负责人（待审批事项）。"""
    leaders = (
        (
            await session.execute(
                select(ProjectMember).where(
                    ProjectMember.project_id == project_id,
                    ProjectMember.role == ROLE_LEADER,
                    ProjectMember.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    for leader in leaders:
        await notify(
            session,
            project_id=project_id,
            recipient_id=leader.id,
            type=type,
            title=title,
            body=body,
            link=link,
            outbox=outbox,
        )


async def _apply_to_target(session: AsyncSession, request: DeadlineChangeRequest) -> None:
    """在当前事务中更新目标 DDL 并递增 `version`。"""
    if request.target_type == DeadlineTargetType.WORK_ITEM:
        target = await get_work_item(session, request.target_id)
    else:
        target = await session.get(CollaborationRequest, request.target_id)
        if target is None:
            raise ApiException(404, ErrorCodes.NOT_FOUND, "目标协作请求不存在")
    target.due_at = request.new_due_at
    target.version += 1
    await session.flush()




async def create_deadline_change_request(
    session: AsyncSession,
    actor: ProjectMember,
    item_id: uuid.UUID,
    payload: DeadlineChangeCreateIn,
) -> DeadlineChangeRequestOut:
    """发起 DDL 变更申请。

    同事务完成：校验目标与发起人权限 → 查重（主任务级）→ 创建申请
    （PENDING_IMPACT_ANALYSIS）→ 同步生成规则化影响分析并推进到
    PENDING_APPROVAL（分析异常标记 unavailable 照常推进）→ 审计 → 通知。
    协作级新 DDL 不影响主任务 DDL 时，同事务直接生效并落 APPROVED。
    `commit` 成功后发布实时事件。
    """
    events: list[OutgoingEvent] = []
    item = await get_work_item(session, item_id, project_id=actor.project_id)  # 越权 → 404

    collab: CollaborationRequest | None = None
    if payload.target_type == DeadlineTargetType.WORK_ITEM:
        if payload.target_id != item.id:
            raise ApiException(
                422, ErrorCodes.VALIDATION_ERROR, "主任务级变更的 target_id 必须是该工作项本身"
            )
        if item.assignee_id != actor.id and actor.role != ROLE_LEADER:
            raise ApiException(
                403, ErrorCodes.FORBIDDEN, "仅工作项当前主执行人或负责人可发起主任务 DDL 变更"
            )
        old_due_at = item.due_at
        # 先行查重提供明确错误，唯一部分索引负责封闭并发窗口。
        pending = (
            await session.execute(
                select(DeadlineChangeRequest.id).where(
                    DeadlineChangeRequest.work_item_id == item.id,
                    DeadlineChangeRequest.target_type == DeadlineTargetType.WORK_ITEM.value,
                    DeadlineChangeRequest.status.in_(
                        [s.value for s in (DeadlineChangeStatus.PENDING_IMPACT_ANALYSIS,
                                           DeadlineChangeStatus.PENDING_APPROVAL)]
                    ),
                )
            )
        ).scalar_one_or_none()
        if pending is not None:
            raise ApiException(
                409,
                ErrorCodes.DEADLINE_CHANGE_PENDING_CONFLICT,
                "该工作项已存在待审批的主 DDL 变更",
                details={"pending_request_id": str(pending)},
            )
    else:
        collab = await session.get(CollaborationRequest, payload.target_id)
        if collab is None or collab.work_item_id != item.id:
            raise ApiException(404, ErrorCodes.NOT_FOUND, "目标协作请求不存在")
        if actor.id not in (collab.requester_id, collab.assignee_id):
            raise ApiException(403, ErrorCodes.FORBIDDEN, "仅协作双方可发起协作 DDL 变更")
        if collab.status in _COLLAB_TERMINAL:
            raise ApiException(
                422, ErrorCodes.VALIDATION_ERROR, "已结束的协作请求不允许变更 DDL"
            )
        old_due_at = collab.due_at

    request = DeadlineChangeRequest(
        target_type=payload.target_type,
        target_id=payload.target_id,
        work_item_id=item.id,
        old_due_at=old_due_at,
        new_due_at=payload.new_due_at,
        reason=payload.reason,
        requested_by=actor.id,
        status=DeadlineChangeStatus.PENDING_IMPACT_ANALYSIS.value,
    )
    session.add(request)
    try:
        await session.flush()
    except IntegrityError:
        # 唯一部分索引阻止并发请求创建第二条待审批主 DDL 变更。
        await session.rollback()
        raise ApiException(
            409,
            ErrorCodes.DEADLINE_CHANGE_PENDING_CONFLICT,
            "该工作项已存在待审批的主 DDL 变更",
        ) from None

    # 影响分析与申请同事务生成；失败时降级，不阻塞人工审批。
    try:
        request.impact_analysis = await generate_impact_analysis(
            session,
            target_type=request.target_type,
            target_id=request.target_id,
            old_due_at=old_due_at,
            new_due_at=request.new_due_at,
            work_item=item,
        )
        request.impact_analysis_status = ImpactAnalysisStatus.GENERATED.value
    except Exception:
        logger.warning("deadline impact analysis failed: request_id=%s", request.id)
        request.impact_analysis = None
        request.impact_analysis_status = ImpactAnalysisStatus.UNAVAILABLE.value
    request.status = transition(request.status, "analyze").value
    await session.flush()

    await record_event(
        session,
        actor_id=actor.user_id,
        action="deadline_change.requested",
        target_type="deadline_change_request",
        target_id=request.id,
        before=None,
        after={
            "work_item_id": str(item.id),
            "target_type": request.target_type,
            "target_id": str(request.target_id),
            "old_due_at": _iso(old_due_at),
            "new_due_at": _iso(request.new_due_at),
            "status": request.status,
            "impact_analysis_status": request.impact_analysis_status,
        },
    )

    # 协作级新 DDL 不晚于主任务 DDL 时由双方直接确认生效。
    auto_approve = (
        request.target_type == DeadlineTargetType.COLLABORATION_REQUEST
        and (item.due_at is None or request.new_due_at <= item.due_at)
    )
    if auto_approve:
        await _apply_to_target(session, request)
        request.status = transition(request.status, "approve").value
        request.approved_by = actor.id
        request.approved_at = datetime.now(UTC)
        await session.flush()
        await record_event(
            session,
            actor_id=actor.user_id,
            action="deadline_change.approved",
            target_type="deadline_change_request",
            target_id=request.id,
            before={"status": DeadlineChangeStatus.PENDING_APPROVAL.value},
            after={"status": request.status, "auto_approved": True},
        )
        assert collab is not None
        other_id = collab.assignee_id if actor.id == collab.requester_id else collab.requester_id
        await notify(
            session,
            project_id=actor.project_id,
            recipient_id=other_id,
            type="deadline_change.approved",
            title="协作 DDL 已变更",
            body=f"{actor.display_name} 将协作请求「{collab.title}」的截止时间变更为 "
            f"{request.new_due_at.strftime('%Y-%m-%d')}（不影响主任务 DDL，已直接生效）",
            link=f"/work-items/{item.id}",
            outbox=events,
        )
    else:
        await _notify_leaders(
            session,
            project_id=item.project_id,
            type="deadline_change.requested",
            title="新的 DDL 变更申请待审批",
            body=f"{actor.display_name} 申请变更工作项「{item.title}」的截止时间",
            link=f"/work-items/{item.id}",
            outbox=events,
        )

    await session.commit()
    await publish_after_commit(events)
    await session.refresh(request)  # created_at/updated_at 由数据库生成，刷新取回
    logger.info(
        "deadline change requested: id=%s, target_type=%s, auto_approved=%s",
        request.id,
        request.target_type,
        auto_approve,
    )
    return await request_to_out(session, request)


async def approve_deadline_change(
    session: AsyncSession,
    actor: ProjectMember,
    request_id: uuid.UUID,
    version: int,
    *,
    decision_note: str | None = None,
) -> DeadlineChangeRequestOut:
    """同事务更新目标 DDL、申请状态、审计和通知，提交后发布事件。"""
    events: list[OutgoingEvent] = []
    request = await get_request(session, request_id, for_update=True, project_id=actor.project_id)
    _check_version(request, version)
    new_status = transition(request.status, "approve")

    await _apply_to_target(session, request)
    request.status = new_status.value
    request.approved_by = actor.id
    request.approved_at = datetime.now(UTC)
    request.version += 1
    await session.flush()

    after: dict[str, object] = {"status": request.status, "approved_by": str(actor.id)}
    if decision_note:
        after["decision_note"] = decision_note  # 审批意见不进入通知正文。
    await record_event(
        session,
        actor_id=actor.user_id,
        action="deadline_change.approved",
        target_type="deadline_change_request",
        target_id=request.id,
        before={"status": DeadlineChangeStatus.PENDING_APPROVAL.value},
        after=after,
    )
    item_title = (
        await session.execute(select(WorkItem.title).where(WorkItem.id == request.work_item_id))
    ).scalar_one()
    await notify(
        session,
        project_id=actor.project_id,
        recipient_id=request.requested_by,
        type="deadline_change.approved",
        title="DDL 变更已通过",
        body=f"工作项「{item_title}」的 DDL 变更申请已通过并生效",
        link=f"/work-items/{request.work_item_id}",
        outbox=events,
    )
    await session.commit()
    await publish_after_commit(events)
    await session.refresh(request)
    logger.info("deadline change approved: id=%s", request.id)
    return await request_to_out(session, request)


async def reject_deadline_change(
    session: AsyncSession,
    actor: ProjectMember,
    request_id: uuid.UUID,
    version: int,
    *,
    decision_note: str | None = None,
) -> DeadlineChangeRequestOut:
    """驳回时不修改目标 DDL；审计和通知同事务提交，随后发布事件。"""
    events: list[OutgoingEvent] = []
    request = await get_request(session, request_id, for_update=True, project_id=actor.project_id)
    _check_version(request, version)
    new_status = transition(request.status, "reject")

    request.status = new_status.value
    request.approved_by = actor.id
    request.approved_at = datetime.now(UTC)
    request.version += 1
    await session.flush()

    after: dict[str, object] = {"status": request.status, "approved_by": str(actor.id)}
    if decision_note:
        after["decision_note"] = decision_note  # 审批意见不进入通知正文。
    await record_event(
        session,
        actor_id=actor.user_id,
        action="deadline_change.rejected",
        target_type="deadline_change_request",
        target_id=request.id,
        before={"status": DeadlineChangeStatus.PENDING_APPROVAL.value},
        after=after,
    )
    item_title = (
        await session.execute(select(WorkItem.title).where(WorkItem.id == request.work_item_id))
    ).scalar_one()
    await notify(
        session,
        project_id=actor.project_id,
        recipient_id=request.requested_by,
        type="deadline_change.rejected",
        title="DDL 变更被驳回",
        body=f"工作项「{item_title}」的 DDL 变更申请已被负责人驳回",
        link=f"/work-items/{request.work_item_id}",
        outbox=events,
    )
    await session.commit()
    await publish_after_commit(events)
    await session.refresh(request)
    logger.info("deadline change rejected: id=%s", request.id)
    return await request_to_out(session, request)


async def cancel_deadline_change(
    session: AsyncSession,
    actor: ProjectMember,
    request_id: uuid.UUID,
    version: int,
) -> DeadlineChangeRequestOut:
    """发起人取消自己的待审批申请，同事务审计。"""
    request = await get_request(session, request_id, for_update=True, project_id=actor.project_id)
    if request.requested_by != actor.id:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅 DDL 变更申请人可取消")
    _check_version(request, version)
    before_status = request.status
    new_status = transition(request.status, "cancel")

    request.status = new_status.value
    request.version += 1
    await session.flush()
    await record_event(
        session,
        actor_id=actor.user_id,
        action="deadline_change.cancelled",
        target_type="deadline_change_request",
        target_id=request.id,
        before={"status": before_status},
        after={"status": request.status},
    )
    await session.commit()
    await session.refresh(request)
    logger.info("deadline change cancelled: id=%s", request.id)
    return await request_to_out(session, request)
