"""协作请求应用服务与权限策略（6.1、7.2、8.2、12.4 节）。

权限规则（16 节，每个用例显式校验）：
- 发起：仅工作项当前主执行人（assignee），无需负责人事前审批（2.1 节）；
- accept / decline / start / submit：仅接收人；
- request_revision / complete：仅发起人；
- cancel：发起人或接收人（8.2 节"双方确认取消"首版简化，见 state_machine.py）；
- 查询：任何项目成员（原则 6 透明）。

核心约束（7.2 节）：协作请求任何状态变化都不得触碰
work_items.assignee_id 与工作项状态（有集成测试断言）。
状态迁移由 state_machine.py 裁决；每次迁移与同事务写审计事件（原则 5），
并按事件向对端写入站内通知（同事务，不丢）。
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiException, ErrorCodes
from app.core.logging import setup_logging
from app.domains.audit.service import record_event
from app.domains.collaboration.models import CollaborationRequest
from app.domains.collaboration.schemas import (
    CollaborationRequestCreateIn,
    CollaborationRequestOut,
    CollaborationRequestSummaryOut,
)
from app.domains.collaboration.state_machine import CollaborationStatus, transition
from app.domains.deliverables.service import get_deliverable, validate_file_reference
from app.domains.notifications.service import notify
from app.domains.project.models import ProjectMember
from app.domains.work_items.models import WorkItem, WorkItemCollaborator
from app.domains.work_items.schemas import MemberBrief
from app.domains.work_items.service import get_work_item, get_work_item_project_id
from app.infrastructure.events import OutgoingEvent, publish_after_commit

logger = setup_logging("backend")

# 命令 → 审计动作名
_COMMAND_AUDIT_ACTION = {
    "accept": "collaboration.accepted",
    "decline": "collaboration.declined",
    "start": "collaboration.started",
    "submit": "collaboration.submitted",
    "request_revision": "collaboration.revision_requested",
    "complete": "collaboration.completed",
    "cancel": "collaboration.cancelled",
}

# 命令 → 触发者要求："requester" 仅发起人，"assignee" 仅接收人，"either" 双方均可
_COMMAND_ACTOR = {
    "accept": "assignee",
    "decline": "assignee",
    "start": "assignee",
    "submit": "assignee",
    "request_revision": "requester",
    "complete": "requester",
    "cancel": "either",
}

# 命令 → 站内通知（接收方、标题）；start/cancel 不产生通知
_COMMAND_NOTIFICATION = {
    "accept": ("requester", "协作请求已被接受"),
    "decline": ("requester", "协作请求已被拒绝"),
    "submit": ("requester", "协作产物已回传"),
    "request_revision": ("assignee", "协作产物需要修改"),
    "complete": ("assignee", "协作请求已完成"),
}


# ---------- 查询与序列化 ----------


async def get_request(
    session: AsyncSession, request_id: uuid.UUID, *, project_id: uuid.UUID | None = None
) -> CollaborationRequest:
    request = await session.get(CollaborationRequest, request_id)
    if request is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "协作请求不存在")
    if project_id is not None and (
        await get_work_item_project_id(session, request.work_item_id) != project_id
    ):
        # 越权 404：项目墙外的请求与不存在等价（spec D3），不泄露存在性信息
        raise ApiException(404, ErrorCodes.NOT_FOUND, "协作请求不存在")
    return request


async def _load_context(
    session: AsyncSession, requests: list[CollaborationRequest]
) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, MemberBrief]]:
    """批量取关联工作项标题与双方成员显示名。"""
    item_ids = {r.work_item_id for r in requests}
    member_ids = {r.requester_id for r in requests} | {r.assignee_id for r in requests}

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


def _brief(briefs: dict[uuid.UUID, MemberBrief], member_id: uuid.UUID) -> MemberBrief:
    return briefs.get(member_id) or MemberBrief(id=member_id, display_name="")


def _to_out(
    request: CollaborationRequest,
    item_titles: dict[uuid.UUID, str],
    briefs: dict[uuid.UUID, MemberBrief],
) -> CollaborationRequestOut:
    return CollaborationRequestOut(
        id=request.id,
        work_item_id=request.work_item_id,
        work_item_title=item_titles.get(request.work_item_id, ""),
        requester=_brief(briefs, request.requester_id),
        assignee=_brief(briefs, request.assignee_id),
        title=request.title,
        goal=request.goal,
        template=request.template,
        due_at=request.due_at,
        result_text=request.result_text,
        result_deliverable_id=request.result_deliverable_id,
        result_file_id=request.result_file_id,
        status=request.status,
        version=request.version,
        created_at=request.created_at,
        updated_at=request.updated_at,
    )


def _to_summary(
    request: CollaborationRequest,
    item_titles: dict[uuid.UUID, str],
    briefs: dict[uuid.UUID, MemberBrief],
) -> CollaborationRequestSummaryOut:
    return CollaborationRequestSummaryOut(
        id=request.id,
        work_item_id=request.work_item_id,
        work_item_title=item_titles.get(request.work_item_id, ""),
        requester=_brief(briefs, request.requester_id),
        assignee=_brief(briefs, request.assignee_id),
        title=request.title,
        status=request.status,
        due_at=request.due_at,
        version=request.version,
        created_at=request.created_at,
        updated_at=request.updated_at,
    )


async def request_to_out(
    session: AsyncSession, request: CollaborationRequest
) -> CollaborationRequestOut:
    item_titles, briefs = await _load_context(session, [request])
    return _to_out(request, item_titles, briefs)


async def get_detail(
    session: AsyncSession, request_id: uuid.UUID, *, project_id: uuid.UUID | None = None
) -> CollaborationRequestOut:
    """单条详情（含 goal/template/result_text 正文，项目成员可查，原则 6 透明）。"""
    request = await get_request(session, request_id, project_id=project_id)
    return await request_to_out(session, request)


async def list_for_work_item(
    session: AsyncSession, item_id: uuid.UUID, *, project_id: uuid.UUID | None = None
) -> list[CollaborationRequestSummaryOut]:
    """某工作项的协作请求列表（项目成员可查，原则 6 透明）。"""
    await get_work_item(session, item_id, project_id=project_id)  # 越权 → 404
    requests = list(
        (
            await session.execute(
                select(CollaborationRequest)
                .where(CollaborationRequest.work_item_id == item_id)
                .order_by(CollaborationRequest.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    item_titles, briefs = await _load_context(session, requests)
    return [_to_summary(r, item_titles, briefs) for r in requests]


async def list_mine(
    session: AsyncSession, actor: ProjectMember, role: str
) -> list[CollaborationRequestSummaryOut]:
    """我的协作（13.2 节）：role=sent 我发出的，role=received 我收到的；
    限定当前项目（spec D2 经工作项推导）。"""
    column = (
        CollaborationRequest.requester_id if role == "sent" else CollaborationRequest.assignee_id
    )
    requests = list(
        (
            await session.execute(
                select(CollaborationRequest)
                .join(WorkItem, WorkItem.id == CollaborationRequest.work_item_id)
                .where(
                    column == actor.id,
                    WorkItem.project_id == actor.project_id,
                )
                .order_by(CollaborationRequest.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    item_titles, briefs = await _load_context(session, requests)
    return [_to_summary(r, item_titles, briefs) for r in requests]


# ---------- 内部工具 ----------


def _check_version(request: CollaborationRequest, version: int) -> None:
    """乐观锁（17.2 节）：客户端携带的 version 与当前不一致即 409。"""
    if request.version != version:
        raise ApiException(
            409,
            ErrorCodes.COLLABORATION_VERSION_CONFLICT,
            "协作请求已被其他成员更新，请刷新后重试",
            details={"current_version": request.version},
        )


def _check_actor(request: CollaborationRequest, actor: ProjectMember, command: str) -> None:
    required = _COMMAND_ACTOR[command]
    if required == "assignee" and request.assignee_id != actor.id:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅协作请求接收人可执行该操作")
    if required == "requester" and request.requester_id != actor.id:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅协作请求发起人可执行该操作")
    if required == "either" and actor.id not in (request.requester_id, request.assignee_id):
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅协作请求双方可执行该操作")


async def _get_active_member(
    session: AsyncSession, member_id: uuid.UUID, *, project_id: uuid.UUID
) -> ProjectMember:
    member = await session.get(ProjectMember, member_id)
    if member is None or not member.is_active:
        raise ApiException(
            422, ErrorCodes.VALIDATION_ERROR, "指定成员不存在或已被禁用", {"member_id": str(member_id)}
        )
    if member.project_id != project_id:
        # spec D3：跨项目成员引用 → 400（成员属于其他项目，不能作为本项目协作接收人）
        raise ApiException(
            400,
            ErrorCodes.CROSS_PROJECT_REFERENCE,
            "不能与其它项目的成员发起协作",
            {"member_id": str(member_id)},
        )
    return member


# ---------- 用例 ----------


async def create_collaboration_request(
    session: AsyncSession,
    actor: ProjectMember,
    item_id: uuid.UUID,
    payload: CollaborationRequestCreateIn,
) -> CollaborationRequestOut:
    """发起协作请求（7.2 节）：仅工作项当前主执行人，无需负责人审批（2.1 节）。

    同事务完成：创建请求（REQUESTED）→ 接收人补入工作项协作者列表（若不在）→
    审计 collaboration.requested → 通知接收人；commit 成功后向接收人发布实时事件。
    """
    events: list[OutgoingEvent] = []
    item = await get_work_item(session, item_id, project_id=actor.project_id)  # 越权 → 404
    if item.assignee_id != actor.id:
        raise ApiException(
            403, ErrorCodes.FORBIDDEN, "仅工作项当前主执行人可发起协作请求"
        )
    if payload.assignee_id == actor.id:
        raise ApiException(
            422, ErrorCodes.VALIDATION_ERROR, "协作请求接收人不能是发起人自己"
        )
    assignee = await _get_active_member(session, payload.assignee_id, project_id=actor.project_id)

    request = CollaborationRequest(
        work_item_id=item.id,
        requester_id=actor.id,
        assignee_id=assignee.id,
        title=payload.title,
        goal=payload.goal,
        template=payload.template,
        due_at=payload.due_at,
        status=CollaborationStatus.REQUESTED.value,
    )
    session.add(request)

    # 接收人加入工作项协作者列表（若不在）；不改变主执行人（7.2 节）
    if all(c.member_id != assignee.id for c in item.collaborators):
        item.collaborators = [
            *item.collaborators,
            WorkItemCollaborator(work_item_id=item.id, member_id=assignee.id),
        ]
    await session.flush()

    await record_event(
        session,
        actor_id=actor.user_id,
        action="collaboration.requested",
        target_type="collaboration_request",
        target_id=request.id,
        before=None,
        after={
            "work_item_id": str(item.id),
            "requester_id": str(actor.id),
            "assignee_id": str(assignee.id),
            "title": request.title,
            "status": request.status,
        },
    )
    await notify(
        session,
        project_id=actor.project_id,
        recipient_id=assignee.id,
        type="collaboration.requested",
        title="新的协作请求",
        body=f"{actor.display_name} 在工作项「{item.title}」中向你发起协作请求「{request.title}」",
        link=f"/work-items/{item.id}",
        outbox=events,
    )
    await session.commit()
    await publish_after_commit(events)
    await session.refresh(request)  # created_at/updated_at 由数据库生成，刷新取回
    logger.info(
        "collaboration requested: id=%s, work_item_id=%s, assignee_id=%s",
        request.id,
        item.id,
        assignee.id,
    )
    return await request_to_out(session, request)


async def run_command(
    session: AsyncSession,
    actor: ProjectMember,
    request_id: uuid.UUID,
    command: str,
    version: int,
    *,
    result_text: str | None = None,
    feedback: str | None = None,
    deliverable_id: uuid.UUID | None = None,
    file_id: uuid.UUID | None = None,
) -> CollaborationRequestOut:
    """状态命令：权限校验 + 状态机 + 乐观锁 + 审计 + 通知（同一事务）。

    绝不触碰 work_items.assignee_id 与工作项状态（7.2 节）。
    submit 可附带交付物/文件引用（T4.4）：校验存在性与归属（须属本工作项）。
    commit 成功后发布实时事件（与通知同内容）。
    """
    events: list[OutgoingEvent] = []
    request = await get_request(session, request_id, project_id=actor.project_id)
    _check_actor(request, actor, command)
    _check_version(request, version)

    if command == "submit" and deliverable_id is not None:
        deliverable = await get_deliverable(session, deliverable_id, project_id=actor.project_id)
        if deliverable.work_item_id != request.work_item_id:
            raise ApiException(
                422,
                ErrorCodes.VALIDATION_ERROR,
                "引用的交付物不属于该协作请求的工作项",
                {"deliverable_id": str(deliverable_id)},
            )
    if command == "submit" and file_id is not None:
        # 文件存在、归属同一工作项（未关联则同事务建立关联）、上传人与工作项有关
        await validate_file_reference(session, request.work_item_id, file_id)

    new_status = transition(request.status, command)
    before_status = request.status
    request.status = new_status.value
    if command == "submit":
        request.result_text = result_text
        request.result_deliverable_id = deliverable_id
        request.result_file_id = file_id
    request.version += 1
    await session.flush()

    after: dict[str, Any] = {"status": request.status}
    if command == "request_revision" and feedback:
        after["feedback"] = feedback  # 反馈只进审计留痕，不进通知正文（16 节）
    if command == "submit":
        if deliverable_id is not None:
            after["result_deliverable_id"] = str(deliverable_id)
        if file_id is not None:
            after["result_file_id"] = str(file_id)
    await record_event(
        session,
        actor_id=actor.user_id,
        action=_COMMAND_AUDIT_ACTION[command],
        target_type="collaboration_request",
        target_id=request.id,
        before={"status": before_status},
        after=after,
    )

    notification_spec = _COMMAND_NOTIFICATION.get(command)
    if notification_spec is not None:
        recipient_side, title = notification_spec
        recipient_id = (
            request.requester_id if recipient_side == "requester" else request.assignee_id
        )
        item_title = (
            await session.execute(
                select(WorkItem.title).where(WorkItem.id == request.work_item_id)
            )
        ).scalar_one()
        await notify(
            session,
            project_id=actor.project_id,
            recipient_id=recipient_id,
            type=_COMMAND_AUDIT_ACTION[command],
            title=title,
            body=f"{actor.display_name}：协作请求「{request.title}」（工作项：{item_title}）",
            link=f"/work-items/{request.work_item_id}",
            outbox=events,
        )

    await session.commit()
    await publish_after_commit(events)
    await session.refresh(request)  # updated_at 由数据库 onupdate 生成，刷新取回
    logger.info(
        "collaboration %s: id=%s, %s -> %s", command, request.id, before_status, request.status
    )
    return await request_to_out(session, request)
