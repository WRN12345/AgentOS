"""转派申请应用服务与权限策略（6.1、7.3、8.3、12.4、17.2 节）。

权限规则（16 节，每个用例显式校验）：
- 发起：仅工作项当前主执行人；接收人必须是项目活跃成员且不是自己；
- approve / reject：仅项目负责人（路由层 get_current_leader）；
- cancel：仅发起人（from_member）；
- 查询：任何项目成员（原则 6 透明）。

核心约束：
- 同一工作项同时只能存在一个 PENDING 转派申请（8.3、17.2 节）：应用层先查重
  返回 409，数据库唯一部分索引（迁移 0006）在并发窗口下兜底；
- 审批通过才在同一个事务中：更新 work_items.assignee_id（version+1）+
  更新申请状态 + 写审计事件（from/to 留痕，支撑"历史负责人完整追溯"）+
  通知新旧负责人；审批前主任务负责人不变化（7.3 节）。
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiException, ErrorCodes
from app.core.logging import setup_logging
from app.domains.audit.service import record_event
from app.domains.notifications.service import notify
from app.domains.project.models import ROLE_LEADER, ProjectMember
from app.domains.transfers.models import TransferRequest
from app.domains.transfers.schemas import (
    TransferRequestCreateIn,
    TransferRequestOut,
    TransferRequestSummaryOut,
)
from app.domains.transfers.state_machine import TransferStatus, transition
from app.domains.work_items.models import WorkItem
from app.domains.work_items.schemas import MemberBrief
from app.domains.work_items.service import get_work_item, get_work_item_project_id
from app.infrastructure.events import OutgoingEvent, publish_after_commit

logger = setup_logging("backend")


# ---------- 查询与序列化 ----------


async def get_request(
    session: AsyncSession,
    request_id: uuid.UUID,
    *,
    for_update: bool = False,
    project_id: uuid.UUID | None = None,
) -> TransferRequest:
    # 写路径 for_update=True（17.2 节）：行锁把并发审批串行化，后到请求在锁后
    # 重读最新已提交版本，应用层版本/状态检查才能挡下重复审批
    request = await session.get(TransferRequest, request_id, with_for_update=for_update)
    if request is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "转派申请不存在")
    if project_id is not None and (
        await get_work_item_project_id(session, request.work_item_id) != project_id
    ):
        # 越权 404：项目墙外的申请与不存在等价（spec D3），不泄露存在性信息
        raise ApiException(404, ErrorCodes.NOT_FOUND, "转派申请不存在")
    return request


async def _load_context(
    session: AsyncSession, requests: list[TransferRequest]
) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, MemberBrief]]:
    """批量取关联工作项标题与相关成员显示名。"""
    item_ids = {r.work_item_id for r in requests}
    member_ids: set[uuid.UUID] = set()
    for r in requests:
        member_ids.update((r.from_member_id, r.to_member_id))
        if r.approved_by is not None:
            member_ids.add(r.approved_by)

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
    return item_titles, briefs


def _brief(briefs: dict[uuid.UUID, MemberBrief], member_id: uuid.UUID | None) -> MemberBrief | None:
    if member_id is None:
        return None
    return briefs.get(member_id) or MemberBrief(id=member_id, display_name="")


def _to_out(
    request: TransferRequest,
    item_titles: dict[uuid.UUID, str],
    briefs: dict[uuid.UUID, MemberBrief],
) -> TransferRequestOut:
    return TransferRequestOut(
        id=request.id,
        work_item_id=request.work_item_id,
        work_item_title=item_titles.get(request.work_item_id, ""),
        from_member=_brief(briefs, request.from_member_id),  # type: ignore[arg-type]
        to_member=_brief(briefs, request.to_member_id),  # type: ignore[arg-type]
        reason=request.reason,
        impact_note=request.impact_note,
        agent_suggestion_id=request.agent_suggestion_id,
        status=request.status,
        approved_by=_brief(briefs, request.approved_by),
        approved_at=request.approved_at,
        version=request.version,
        created_at=request.created_at,
        updated_at=request.updated_at,
    )


def _to_summary(
    request: TransferRequest,
    item_titles: dict[uuid.UUID, str],
    briefs: dict[uuid.UUID, MemberBrief],
) -> TransferRequestSummaryOut:
    return TransferRequestSummaryOut(
        id=request.id,
        work_item_id=request.work_item_id,
        work_item_title=item_titles.get(request.work_item_id, ""),
        from_member=_brief(briefs, request.from_member_id),  # type: ignore[arg-type]
        to_member=_brief(briefs, request.to_member_id),  # type: ignore[arg-type]
        status=request.status,
        version=request.version,
        created_at=request.created_at,
        updated_at=request.updated_at,
    )


async def request_to_out(session: AsyncSession, request: TransferRequest) -> TransferRequestOut:
    item_titles, briefs = await _load_context(session, [request])
    return _to_out(request, item_titles, briefs)


async def get_detail(
    session: AsyncSession, request_id: uuid.UUID, *, project_id: uuid.UUID | None = None
) -> TransferRequestOut:
    """单条详情（含 reason/impact_note 正文，项目成员可查，原则 6 透明）。"""
    request = await get_request(session, request_id, project_id=project_id)
    return await request_to_out(session, request)


async def list_for_work_item(
    session: AsyncSession, item_id: uuid.UUID, *, project_id: uuid.UUID | None = None
) -> list[TransferRequestSummaryOut]:
    """某工作项的转派申请历史（项目成员可查，原则 6 透明）。"""
    await get_work_item(session, item_id, project_id=project_id)  # 越权 → 404
    requests = list(
        (
            await session.execute(
                select(TransferRequest)
                .where(TransferRequest.work_item_id == item_id)
                .order_by(TransferRequest.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    item_titles, briefs = await _load_context(session, requests)
    return [_to_summary(r, item_titles, briefs) for r in requests]


async def list_mine(session: AsyncSession, actor: ProjectMember) -> list[TransferRequestSummaryOut]:
    """我发起的转派申请（13.2 节），限定当前项目（spec D2 经工作项推导）。"""
    requests = list(
        (
            await session.execute(
                select(TransferRequest)
                .join(WorkItem, WorkItem.id == TransferRequest.work_item_id)
                .where(
                    TransferRequest.from_member_id == actor.id,
                    WorkItem.project_id == actor.project_id,
                )
                .order_by(TransferRequest.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    item_titles, briefs = await _load_context(session, requests)
    return [_to_summary(r, item_titles, briefs) for r in requests]


async def list_pending(
    session: AsyncSession, *, project_id: uuid.UUID | None = None
) -> list[TransferRequest]:
    """负责人待审批聚合用：当前项目全部 PENDING 转派申请（12.6 节）。"""
    stmt = (
        select(TransferRequest)
        .join(WorkItem, WorkItem.id == TransferRequest.work_item_id)
        .where(TransferRequest.status == TransferStatus.PENDING.value)
        .order_by(TransferRequest.created_at.desc())
    )
    if project_id is not None:
        stmt = stmt.where(WorkItem.project_id == project_id)
    return list((await session.execute(stmt)).scalars().all())


# ---------- 内部工具 ----------


def _check_version(request: TransferRequest, version: int) -> None:
    """乐观锁（17.2 节）：客户端携带的 version 与当前不一致即 409。"""
    if request.version != version:
        raise ApiException(
            409,
            ErrorCodes.TRANSFER_VERSION_CONFLICT,
            "转派申请已被其他成员更新，请刷新后重试",
            details={"current_version": request.version},
        )


async def _get_active_member(
    session: AsyncSession, member_id: uuid.UUID, *, project_id: uuid.UUID
) -> ProjectMember:
    member = await session.get(ProjectMember, member_id)
    if member is None or not member.is_active:
        raise ApiException(
            422, ErrorCodes.VALIDATION_ERROR, "指定成员不存在或已被禁用", {"member_id": str(member_id)}
        )
    if member.project_id != project_id:
        # spec D3：跨项目成员引用 → 400（成员属于其他项目，不能作为本项目接收人）
        raise ApiException(
            400,
            ErrorCodes.CROSS_PROJECT_REFERENCE,
            "不能转派给其他项目的成员",
            {"member_id": str(member_id)},
        )
    return member


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
            recipient_id=leader.id,
            type=type,
            title=title,
            body=body,
            link=link,
            outbox=outbox,
        )


# ---------- 用例 ----------


async def create_transfer_request(
    session: AsyncSession,
    actor: ProjectMember,
    item_id: uuid.UUID,
    payload: TransferRequestCreateIn,
) -> TransferRequestOut:
    """发起转派申请（7.3 节）：仅工作项当前主执行人。

    同事务完成：查重（同工作项已有 PENDING → 409）→ 创建申请 →
    审计 transfer.requested → 通知负责人待审批；commit 后向负责人发布实时事件。
    """
    events: list[OutgoingEvent] = []
    item = await get_work_item(session, item_id, project_id=actor.project_id)  # 越权 → 404
    if item.assignee_id != actor.id:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅工作项当前主执行人可发起转派申请")
    if payload.to_member_id == actor.id:
        raise ApiException(422, ErrorCodes.VALIDATION_ERROR, "转派目标不能是发起人自己")
    to_member = await _get_active_member(
        session, payload.to_member_id, project_id=actor.project_id
    )

    # 同一工作项同时只能存在一个 PENDING（8.3、17.2 节）；唯一部分索引兜底
    pending = (
        await session.execute(
            select(TransferRequest.id).where(
                TransferRequest.work_item_id == item.id,
                TransferRequest.status == TransferStatus.PENDING.value,
            )
        )
    ).scalar_one_or_none()
    if pending is not None:
        raise ApiException(
            409,
            ErrorCodes.TRANSFER_PENDING_CONFLICT,
            "该工作项已存在待审批的转派申请",
            details={"pending_request_id": str(pending)},
        )

    request = TransferRequest(
        work_item_id=item.id,
        from_member_id=actor.id,
        to_member_id=to_member.id,
        reason=payload.reason,
        impact_note=payload.impact_note,
        status=TransferStatus.PENDING.value,
    )
    session.add(request)
    try:
        await session.flush()
    except IntegrityError:
        # 并发窗口下另一请求抢先创建 PENDING（唯一部分索引兜底）
        await session.rollback()
        raise ApiException(
            409,
            ErrorCodes.TRANSFER_PENDING_CONFLICT,
            "该工作项已存在待审批的转派申请",
        ) from None

    await record_event(
        session,
        actor_id=actor.user_id,
        action="transfer.requested",
        target_type="transfer_request",
        target_id=request.id,
        before=None,
        after={
            "work_item_id": str(item.id),
            "from_member_id": str(actor.id),
            "to_member_id": str(to_member.id),
            "status": request.status,
        },
    )
    await _notify_leaders(
        session,
        project_id=item.project_id,
        type="transfer.requested",
        title="新的转派申请待审批",
        body=f"{actor.display_name} 申请将工作项「{item.title}」转派给 {to_member.display_name}",
        link=f"/work-items/{item.id}",
        outbox=events,
    )
    await session.commit()
    await publish_after_commit(events)
    await session.refresh(request)  # created_at/updated_at 由数据库生成，刷新取回
    logger.info(
        "transfer requested: id=%s, work_item_id=%s, to_member_id=%s",
        request.id,
        item.id,
        to_member.id,
    )
    return await request_to_out(session, request)


async def approve_transfer(
    session: AsyncSession,
    actor: ProjectMember,
    request_id: uuid.UUID,
    version: int,
    *,
    decision_note: str | None = None,
) -> TransferRequestOut:
    """负责人审批通过（7.3 节）：同一事务中更新负责人、申请状态、审计与通知。

    审批前 work_items.assignee_id 不变化；通过后 assignee 变更与审计事件
    同生共死，from/to 完整留痕（第 22 章标准 2"历史负责人完整追溯"）。
    幂等键 + 乐观锁 + 状态机共同保证重复审批只生效一次（17.2 节）。
    commit 后向新旧负责人发布实时事件。
    """
    events: list[OutgoingEvent] = []
    request = await get_request(session, request_id, for_update=True, project_id=actor.project_id)
    _check_version(request, version)
    new_status = transition(request.status, "approve")

    # 审批时点再校验目标成员仍为活跃成员，避免转给已禁用账号
    to_member = await _get_active_member(session, request.to_member_id, project_id=actor.project_id)
    item = await get_work_item(session, request.work_item_id, project_id=actor.project_id)

    before_assignee = item.assignee_id
    item.assignee_id = to_member.id
    item.version += 1

    request.status = new_status.value
    request.approved_by = actor.id
    request.approved_at = datetime.now(UTC)
    request.version += 1
    await session.flush()

    # 申请侧审计（含审批意见）
    after: dict[str, object] = {
        "status": request.status,
        "approved_by": str(actor.id),
        "from_member_id": str(request.from_member_id),
        "to_member_id": str(request.to_member_id),
    }
    if decision_note:
        after["decision_note"] = decision_note  # 意见只进审计留痕，不进通知正文（16 节）
    await record_event(
        session,
        actor_id=actor.user_id,
        action="transfer.approved",
        target_type="transfer_request",
        target_id=request.id,
        before={"status": TransferStatus.PENDING.value},
        after=after,
    )
    # 工作项侧审计：负责人变更留痕，支撑历史负责人完整追溯
    await record_event(
        session,
        actor_id=actor.user_id,
        action="work_item.assignee_changed",
        target_type="work_item",
        target_id=item.id,
        before={"assignee_id": str(before_assignee)},
        after={"assignee_id": str(to_member.id), "transfer_request_id": str(request.id)},
    )

    await notify(
        session,
        recipient_id=request.from_member_id,
        type="transfer.approved",
        title="转派申请已通过",
        body=f"工作项「{item.title}」已转派给 {to_member.display_name}",
        link=f"/work-items/{item.id}",
        outbox=events,
    )
    await notify(
        session,
        recipient_id=to_member.id,
        type="transfer.approved",
        title="你已成为工作项主执行人",
        body=f"工作项「{item.title}」经负责人审批转派给你",
        link=f"/work-items/{item.id}",
        outbox=events,
    )
    await session.commit()
    await publish_after_commit(events)
    await session.refresh(request)
    logger.info("transfer approved: id=%s, work_item_id=%s", request.id, item.id)
    return await request_to_out(session, request)


async def reject_transfer(
    session: AsyncSession,
    actor: ProjectMember,
    request_id: uuid.UUID,
    version: int,
    *,
    decision_note: str | None = None,
) -> TransferRequestOut:
    """负责人驳回：主任务负责人不变化（7.3 节），同事务审计 + 通知发起人；commit 后发布事件。"""
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
        after["decision_note"] = decision_note  # 意见只进审计留痕，不进通知正文（16 节）
    await record_event(
        session,
        actor_id=actor.user_id,
        action="transfer.rejected",
        target_type="transfer_request",
        target_id=request.id,
        before={"status": TransferStatus.PENDING.value},
        after=after,
    )
    item_title = (
        await session.execute(select(WorkItem.title).where(WorkItem.id == request.work_item_id))
    ).scalar_one()
    await notify(
        session,
        recipient_id=request.from_member_id,
        type="transfer.rejected",
        title="转派申请被驳回",
        body=f"工作项「{item_title}」的转派申请已被负责人驳回",
        link=f"/work-items/{request.work_item_id}",
        outbox=events,
    )
    await session.commit()
    await publish_after_commit(events)
    await session.refresh(request)
    logger.info("transfer rejected: id=%s", request.id)
    return await request_to_out(session, request)


async def cancel_transfer(
    session: AsyncSession,
    actor: ProjectMember,
    request_id: uuid.UUID,
    version: int,
) -> TransferRequestOut:
    """发起人取消自己的 PENDING 申请，同事务审计。"""
    request = await get_request(session, request_id, for_update=True, project_id=actor.project_id)
    if request.from_member_id != actor.id:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅转派申请发起人可取消")
    _check_version(request, version)
    new_status = transition(request.status, "cancel")

    request.status = new_status.value
    request.version += 1
    await session.flush()
    await record_event(
        session,
        actor_id=actor.user_id,
        action="transfer.cancelled",
        target_type="transfer_request",
        target_id=request.id,
        before={"status": TransferStatus.PENDING.value},
        after={"status": request.status},
    )
    await session.commit()
    await session.refresh(request)
    logger.info("transfer cancelled: id=%s", request.id)
    return await request_to_out(session, request)
