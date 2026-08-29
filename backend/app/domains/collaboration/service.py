"""协作请求应用服务与权限策略。

每个用例显式校验权限：仅工作项当前主执行人可发起；`accept`、`decline`、
`start` 和 `submit` 仅接收人可执行；`request_revision` 和 `complete` 仅发起人
可执行；`cancel` 允许双方执行。

协作请求的任何状态变化都不得修改 `work_items.assignee_id` 或工作项状态。
状态机、审计事件和站内通知在同一事务中处理，避免业务状态与通知不一致。
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

# 状态命令对应的审计动作。
_COMMAND_AUDIT_ACTION = {
    "accept": "collaboration.accepted",
    "decline": "collaboration.declined",
    "start": "collaboration.started",
    "submit": "collaboration.submitted",
    "request_revision": "collaboration.revision_requested",
    "complete": "collaboration.completed",
    "cancel": "collaboration.cancelled",
}

# 状态命令对应的执行方限制。
_COMMAND_ACTOR = {
    "accept": "assignee",
    "decline": "assignee",
    "start": "assignee",
    "submit": "assignee",
    "request_revision": "requester",
    "complete": "requester",
    "cancel": "either",
}

# 状态命令对应的通知接收方和标题；`start` 与 `cancel` 不发送通知。
_COMMAND_NOTIFICATION = {
    "accept": ("requester", "协作请求已被接受"),
    "decline": ("requester", "协作请求已被拒绝"),
    "submit": ("requester", "协作产物已回传"),
    "request_revision": ("assignee", "协作产物需要修改"),
    "complete": ("assignee", "协作请求已完成"),
}




async def get_request(
    session: AsyncSession, request_id: uuid.UUID, *, project_id: uuid.UUID | None = None
) -> CollaborationRequest:
    request = await session.get(CollaborationRequest, request_id)
    if request is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "协作请求不存在")
    if project_id is not None and (
        await get_work_item_project_id(session, request.work_item_id) != project_id
    ):
        # 将跨项目访问视为资源不存在，避免泄露资源存在性。
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
    """返回项目成员可见的详情，包括 `goal`、`template` 和 `result_text`。"""
    request = await get_request(session, request_id, project_id=project_id)
    return await request_to_out(session, request)


async def list_for_work_item(
    session: AsyncSession, item_id: uuid.UUID, *, project_id: uuid.UUID | None = None
) -> list[CollaborationRequestSummaryOut]:
    """返回项目成员可见的工作项协作请求列表。"""
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
    """返回当前项目中本人发出或收到的协作请求。"""
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




def _check_version(request: CollaborationRequest, version: int) -> None:
    """校验乐观锁版本，不一致时返回 `409`。"""
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
        # 显式拒绝跨项目成员引用，防止协作关系突破项目边界。
        raise ApiException(
            400,
            ErrorCodes.CROSS_PROJECT_REFERENCE,
            "不能与其它项目的成员发起协作",
            {"member_id": str(member_id)},
        )
    return member




async def create_collaboration_request(
    session: AsyncSession,
    actor: ProjectMember,
    item_id: uuid.UUID,
    payload: CollaborationRequestCreateIn,
) -> CollaborationRequestOut:
    """由工作项当前主执行人直接发起协作请求。

    创建请求、补充协作者、写审计和通知在同一事务内完成；`commit` 成功后才发布实时事件。
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

    # 接收人加入协作者列表，但不能改变工作项主执行人。
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
    """在同一事务内完成权限、状态机、乐观锁、审计和通知处理。

    不修改 `work_items.assignee_id` 或工作项状态。`submit` 引用的交付物或文件
    必须属于当前工作项；`commit` 成功后才发布实时事件。
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
        # 文件必须可安全关联到当前工作项，关联关系与命令在同一事务内写入。
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
        after["feedback"] = feedback  # 反馈只写入审计记录，不进入通知正文。
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
