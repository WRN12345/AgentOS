"""DDL 变更申请应用服务与权限策略（6.1、7.4、8.4、12.4、17.2 节）。

权限规则（16 节，每个用例显式校验）：
- 发起主任务级（target_type=work_item）：仅工作项当前主执行人或项目负责人，一律负责人审批；
- 发起协作级（target_type=collaboration_request）：仅协作双方（发起人或接收人）；
- approve / reject：仅项目负责人（路由层 get_current_leader）；
- cancel：仅发起人；
- 查询：任何项目成员（原则 6 透明）。

业务规则（7.4 节）：
- 协作级新 DDL 不影响主任务 DDL（新协作 DDL ≤ 主工作项 DDL 或主工作项无 DDL）时，
  由双方直接确认生效：同事务更新 collaboration_requests.due_at 并直接落 APPROVED
  （auto-approved，审计留痕，无需负责人）；
- 影响主任务 DDL 的协作级变更与一切主任务级变更走负责人审批流；
- 同一工作项只能有一个待审批主 DDL 变更（17.2 节）：应用层先查重返回 409，
  数据库唯一部分索引（迁移 0006）在并发窗口下兜底；
- 规则化影响分析在创建时同步生成并推进到 PENDING_APPROVAL；分析异常时
  impact_analysis_status=unavailable 照常推进，不阻塞人工审批（8.4 节）。
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
from app.domains.project.service import get_default_project
from app.domains.work_items.models import WorkItem
from app.domains.work_items.schemas import MemberBrief
from app.domains.work_items.service import get_work_item
from app.infrastructure.events import OutgoingEvent, publish_after_commit

logger = setup_logging("backend")

# 协作请求终态：这些状态下不允许再变更协作 DDL
_COLLAB_TERMINAL = (
    CollaborationStatus.DECLINED.value,
    CollaborationStatus.CANCELLED.value,
    CollaborationStatus.COMPLETED.value,
)


# ---------- 规则化影响分析 ----------


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
    """规则化影响分析（7.4、8.4 节）：同步快操作，创建申请时同事务生成。

    内容：目标新旧 DDL 对比、是否晚于主任务 DDL、该工作项下未完成协作请求
    及其 DDL（受影响对象清单）。阶段 5 在此接入 AI 分析；分析失败由调用方
    捕获并标记 unavailable，不阻塞人工审批。
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


# ---------- 查询与序列化 ----------


async def get_request(
    session: AsyncSession, request_id: uuid.UUID, *, for_update: bool = False
) -> DeadlineChangeRequest:
    # 写路径 for_update=True（17.2 节）：行锁把并发审批串行化，后到请求在锁后
    # 重读最新已提交版本，应用层版本/状态检查才能挡下重复审批
    request = await session.get(DeadlineChangeRequest, request_id, with_for_update=for_update)
    if request is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "DDL 变更申请不存在")
    return request


async def _target_titles(
    session: AsyncSession, requests: list[DeadlineChangeRequest]
) -> dict[uuid.UUID, str]:
    """目标对象标题：work_item → 工作项标题；collaboration_request → 协作请求标题。"""
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


async def get_detail(session: AsyncSession, request_id: uuid.UUID) -> DeadlineChangeRequestOut:
    """单条详情（含 reason 与 impact_analysis 正文，项目成员可查，原则 6 透明）。"""
    request = await get_request(session, request_id)
    return await request_to_out(session, request)


async def list_for_work_item(
    session: AsyncSession, item_id: uuid.UUID
) -> list[DeadlineChangeSummaryOut]:
    """某工作项的 DDL 变更申请历史（项目成员可查，原则 6 透明）。"""
    await get_work_item(session, item_id)  # 工作项不存在 → 404
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
    """我发起的 DDL 变更申请（13.2 节）。"""
    requests = list(
        (
            await session.execute(
                select(DeadlineChangeRequest)
                .where(DeadlineChangeRequest.requested_by == actor.id)
                .order_by(DeadlineChangeRequest.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    item_titles, target_titles, briefs = await _load_context(session, requests)
    return [_to_summary(r, item_titles, target_titles, briefs) for r in requests]


async def list_pending_approval(session: AsyncSession) -> list[DeadlineChangeRequest]:
    """负责人待审批聚合用：全部 PENDING_APPROVAL 的 DDL 变更申请（12.6 节）。"""
    return list(
        (
            await session.execute(
                select(DeadlineChangeRequest)
                .where(
                    DeadlineChangeRequest.status == DeadlineChangeStatus.PENDING_APPROVAL.value
                )
                .order_by(DeadlineChangeRequest.created_at.desc())
            )
        )
        .scalars()
        .all()
    )


# ---------- 内部工具 ----------


def _check_version(request: DeadlineChangeRequest, version: int) -> None:
    """乐观锁（17.2 节）：客户端携带的 version 与当前不一致即 409。"""
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
    type: str,
    title: str,
    body: str,
    link: str,
    outbox: list[OutgoingEvent] | None = None,
) -> None:
    """通知全体活跃项目负责人（待审批事项）。"""
    project = await get_default_project(session)
    leaders = (
        (
            await session.execute(
                select(ProjectMember).where(
                    ProjectMember.project_id == project.id,
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
            recipient_id=leader.id,
            type=type,
            title=title,
            body=body,
            link=link,
            outbox=outbox,
        )


async def _apply_to_target(session: AsyncSession, request: DeadlineChangeRequest) -> None:
    """审批生效：同事务更新目标 DDL 并 version+1（17.2 节）。"""
    if request.target_type == DeadlineTargetType.WORK_ITEM:
        target = await get_work_item(session, request.target_id)
    else:
        target = await session.get(CollaborationRequest, request.target_id)
        if target is None:
            raise ApiException(404, ErrorCodes.NOT_FOUND, "目标协作请求不存在")
    target.due_at = request.new_due_at
    target.version += 1
    await session.flush()


# ---------- 用例 ----------


async def create_deadline_change_request(
    session: AsyncSession,
    actor: ProjectMember,
    item_id: uuid.UUID,
    payload: DeadlineChangeCreateIn,
) -> DeadlineChangeRequestOut:
    """发起 DDL 变更申请（7.4 节）。

    同事务完成：校验目标与发起人权限 → 查重（主任务级）→ 创建申请
    （PENDING_IMPACT_ANALYSIS）→ 同步生成规则化影响分析并推进到
    PENDING_APPROVAL（分析异常标记 unavailable 照常推进）→ 审计 → 通知。
    协作级新 DDL 不影响主任务 DDL 时，同事务直接生效并落 APPROVED。
    commit 成功后发布实时事件。
    """
    events: list[OutgoingEvent] = []
    item = await get_work_item(session, item_id)

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
        # 同一工作项只能有一个待审批主 DDL 变更（17.2 节）；唯一部分索引兜底
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
        # 并发窗口下另一请求抢先创建待审批主 DDL 变更（唯一部分索引兜底）
        await session.rollback()
        raise ApiException(
            409,
            ErrorCodes.DEADLINE_CHANGE_PENDING_CONFLICT,
            "该工作项已存在待审批的主 DDL 变更",
        ) from None

    # 规则化影响分析：同步快操作，同事务生成；失败不阻塞人工审批（8.4 节）
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

    # 协作级：新 DDL 不影响主任务 DDL 时双方直接确认生效（7.4 节）
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
        # 通知协作对端：DDL 已变更生效
        assert collab is not None
        other_id = collab.assignee_id if actor.id == collab.requester_id else collab.requester_id
        await notify(
            session,
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
    """负责人审批通过：同事务更新目标 DDL（version+1）+ 申请状态 + 审计 + 通知；commit 后发布事件。"""
    events: list[OutgoingEvent] = []
    request = await get_request(session, request_id, for_update=True)
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
        after["decision_note"] = decision_note  # 意见只进审计留痕，不进通知正文（16 节）
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
    """负责人驳回：目标 DDL 不变化，同事务审计 + 通知发起人；commit 后发布事件。"""
    events: list[OutgoingEvent] = []
    request = await get_request(session, request_id, for_update=True)
    _check_version(request, version)
    new_status = transition(request.status, "reject")

    request.status = new_status.value
    request.approved_by = actor.id
    request.approved_at = datetime.now(UTC)
    request.version += 1
    await session.flush()

    after: dict[str, object] = {"status": request.status, "approved_by": str(actor.id)}
    if decision_note:
        after["decision_note"] = decision_note  # 意见只进审计留痕，不进通知正文（16 节）
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
    request = await get_request(session, request_id, for_update=True)
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
