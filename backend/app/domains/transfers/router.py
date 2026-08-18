"""转派申请接口（12.4 节）。

- POST /work-items/{id}/transfer-requests   仅工作项当前主执行人：发起转派申请
- GET  /work-items/{id}/transfer-requests   任何成员：该工作项的转派申请历史
- GET  /transfer-requests?role=mine         本人：我发起的转派申请摘要
- GET  /transfer-requests/{id}              任何成员：单条详情（含 reason/impact_note）
- POST /transfer-requests/{id}/approve      仅负责人：PENDING → APPROVED（同事务更新主执行人）
- POST /transfer-requests/{id}/reject       仅负责人：PENDING → REJECTED
- POST /transfer-requests/{id}/cancel       仅发起人：PENDING → CANCELLED

所有写接口支持 Idempotency-Key，且要求携带 version 做乐观锁（17.2 节）。
approve/reject 可携带 decision_note（审批意见，只入审计不进通知，16 节）。
"""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.idempotency import idempotency_guard
from app.domains.project.dependencies import get_current_leader, get_current_member
from app.domains.project.models import ProjectMember
from app.domains.transfers.schemas import (
    TransferCommandIn,
    TransferDecisionIn,
    TransferRequestCreateIn,
    TransferRequestOut,
    TransferRequestSummaryOut,
)
from app.domains.transfers.service import (
    approve_transfer,
    cancel_transfer,
    create_transfer_request,
    get_detail,
    list_for_work_item,
    list_mine,
    reject_transfer,
)
from app.infrastructure.database.engine import get_session

router = APIRouter(tags=["transfers"])


@router.post(
    "/work-items/{item_id}/transfer-requests",
    response_model=TransferRequestOut,
    status_code=201,
)
async def create_transfer_request_endpoint(
    item_id: uuid.UUID,
    payload: TransferRequestCreateIn,
    actor: ProjectMember = Depends(get_current_member),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> TransferRequestOut:
    return await create_transfer_request(session, actor, item_id, payload)


@router.get(
    "/work-items/{item_id}/transfer-requests",
    response_model=list[TransferRequestSummaryOut],
)
async def list_work_item_transfer_requests_endpoint(
    item_id: uuid.UUID,
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> list[TransferRequestSummaryOut]:
    return await list_for_work_item(session, item_id, project_id=actor.project_id)


@router.get("/transfer-requests", response_model=list[TransferRequestSummaryOut])
async def list_my_transfer_requests_endpoint(
    role: Literal["mine"] = Query(),
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> list[TransferRequestSummaryOut]:
    return await list_mine(session, actor)


@router.get("/transfer-requests/{request_id}", response_model=TransferRequestOut)
async def get_transfer_request_endpoint(
    request_id: uuid.UUID,
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> TransferRequestOut:
    return await get_detail(session, request_id, project_id=actor.project_id)


@router.post("/transfer-requests/{request_id}/approve", response_model=TransferRequestOut)
async def approve_endpoint(
    request_id: uuid.UUID,
    payload: TransferDecisionIn,
    actor: ProjectMember = Depends(get_current_leader),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> TransferRequestOut:
    return await approve_transfer(
        session, actor, request_id, payload.version, decision_note=payload.decision_note
    )


@router.post("/transfer-requests/{request_id}/reject", response_model=TransferRequestOut)
async def reject_endpoint(
    request_id: uuid.UUID,
    payload: TransferDecisionIn,
    actor: ProjectMember = Depends(get_current_leader),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> TransferRequestOut:
    return await reject_transfer(
        session, actor, request_id, payload.version, decision_note=payload.decision_note
    )


@router.post("/transfer-requests/{request_id}/cancel", response_model=TransferRequestOut)
async def cancel_endpoint(
    request_id: uuid.UUID,
    payload: TransferCommandIn,
    actor: ProjectMember = Depends(get_current_member),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> TransferRequestOut:
    return await cancel_transfer(session, actor, request_id, payload.version)
