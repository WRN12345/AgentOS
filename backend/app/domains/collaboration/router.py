"""协作请求接口（12.4 节）。

- POST /work-items/{id}/collaboration-requests   仅工作项当前主执行人：发起（无需负责人审批，2.1 节）
- GET  /work-items/{id}/collaboration-requests   任何成员：该工作项的协作请求列表
- GET  /collaboration-requests?role=sent|received 本人：我发出/收到的协作请求摘要（13.2 节）
- GET  /collaboration-requests/{id}            任何成员：单条详情（含 goal/template/result_text）
- POST /collaboration-requests/{id}/accept           仅接收人：REQUESTED → ACCEPTED
- POST /collaboration-requests/{id}/decline          仅接收人：REQUESTED → DECLINED
- POST /collaboration-requests/{id}/start            仅接收人：ACCEPTED/REVISION_REQUESTED → IN_PROGRESS
                                                   （12.4 节未单列此端点，8.2 状态机需要"开始处理/继续处理"，
                                                    故补充并与"继续处理"共用）
- POST /collaboration-requests/{id}/submit           仅接收人：IN_PROGRESS → SUBMITTED（携带 result_text）
- POST /collaboration-requests/{id}/request-revision 仅发起人：SUBMITTED → REVISION_REQUESTED（可带反馈）
- POST /collaboration-requests/{id}/complete         仅发起人：SUBMITTED → COMPLETED
- POST /collaboration-requests/{id}/cancel           发起人或接收人：REQUESTED/ACCEPTED → CANCELLED

所有写接口支持 Idempotency-Key，且要求携带 version 做乐观锁（17.2 节）。
"""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.idempotency import idempotency_guard
from app.domains.collaboration.schemas import (
    CollaborationCommandIn,
    CollaborationRequestCreateIn,
    CollaborationRequestOut,
    CollaborationRequestSummaryOut,
    CollaborationRevisionIn,
    CollaborationSubmitIn,
)
from app.domains.collaboration.service import (
    create_collaboration_request,
    get_detail,
    list_for_work_item,
    list_mine,
    run_command,
)
from app.domains.project.dependencies import get_current_member
from app.domains.project.models import ProjectMember
from app.infrastructure.database.engine import get_session

router = APIRouter(tags=["collaboration"])


@router.post(
    "/work-items/{item_id}/collaboration-requests",
    response_model=CollaborationRequestOut,
    status_code=201,
)
async def create_collaboration_request_endpoint(
    item_id: uuid.UUID,
    payload: CollaborationRequestCreateIn,
    actor: ProjectMember = Depends(get_current_member),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> CollaborationRequestOut:
    return await create_collaboration_request(session, actor, item_id, payload)


@router.get(
    "/work-items/{item_id}/collaboration-requests",
    response_model=list[CollaborationRequestSummaryOut],
)
async def list_work_item_collaboration_requests_endpoint(
    item_id: uuid.UUID,
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> list[CollaborationRequestSummaryOut]:
    return await list_for_work_item(session, item_id, project_id=actor.project_id)


@router.get(
    "/collaboration-requests",
    response_model=list[CollaborationRequestSummaryOut],
)
async def list_my_collaboration_requests_endpoint(
    role: Literal["sent", "received"] = Query(),
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> list[CollaborationRequestSummaryOut]:
    return await list_mine(session, actor, role)


@router.get(
    "/collaboration-requests/{request_id}",
    response_model=CollaborationRequestOut,
)
async def get_collaboration_request_endpoint(
    request_id: uuid.UUID,
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> CollaborationRequestOut:
    return await get_detail(session, request_id, project_id=actor.project_id)


@router.post(
    "/collaboration-requests/{request_id}/accept",
    response_model=CollaborationRequestOut,
)
async def accept_endpoint(
    request_id: uuid.UUID,
    payload: CollaborationCommandIn,
    actor: ProjectMember = Depends(get_current_member),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> CollaborationRequestOut:
    return await run_command(session, actor, request_id, "accept", payload.version)


@router.post(
    "/collaboration-requests/{request_id}/decline",
    response_model=CollaborationRequestOut,
)
async def decline_endpoint(
    request_id: uuid.UUID,
    payload: CollaborationCommandIn,
    actor: ProjectMember = Depends(get_current_member),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> CollaborationRequestOut:
    return await run_command(session, actor, request_id, "decline", payload.version)


@router.post(
    "/collaboration-requests/{request_id}/start",
    response_model=CollaborationRequestOut,
)
async def start_endpoint(
    request_id: uuid.UUID,
    payload: CollaborationCommandIn,
    actor: ProjectMember = Depends(get_current_member),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> CollaborationRequestOut:
    return await run_command(session, actor, request_id, "start", payload.version)


@router.post(
    "/collaboration-requests/{request_id}/submit",
    response_model=CollaborationRequestOut,
)
async def submit_endpoint(
    request_id: uuid.UUID,
    payload: CollaborationSubmitIn,
    actor: ProjectMember = Depends(get_current_member),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> CollaborationRequestOut:
    return await run_command(
        session,
        actor,
        request_id,
        "submit",
        payload.version,
        result_text=payload.result_text,
        deliverable_id=payload.deliverable_id,
        file_id=payload.file_id,
    )


@router.post(
    "/collaboration-requests/{request_id}/request-revision",
    response_model=CollaborationRequestOut,
)
async def request_revision_endpoint(
    request_id: uuid.UUID,
    payload: CollaborationRevisionIn,
    actor: ProjectMember = Depends(get_current_member),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> CollaborationRequestOut:
    return await run_command(
        session,
        actor,
        request_id,
        "request_revision",
        payload.version,
        feedback=payload.feedback,
    )


@router.post(
    "/collaboration-requests/{request_id}/complete",
    response_model=CollaborationRequestOut,
)
async def complete_endpoint(
    request_id: uuid.UUID,
    payload: CollaborationCommandIn,
    actor: ProjectMember = Depends(get_current_member),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> CollaborationRequestOut:
    return await run_command(session, actor, request_id, "complete", payload.version)


@router.post(
    "/collaboration-requests/{request_id}/cancel",
    response_model=CollaborationRequestOut,
)
async def cancel_endpoint(
    request_id: uuid.UUID,
    payload: CollaborationCommandIn,
    actor: ProjectMember = Depends(get_current_member),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> CollaborationRequestOut:
    return await run_command(session, actor, request_id, "cancel", payload.version)
