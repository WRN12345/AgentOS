"""DDL 变更申请接口（12.4 节）。

- POST /work-items/{id}/deadline-change-requests   协作级：协作双方；主任务级：主执行人或负责人
- GET  /work-items/{id}/deadline-change-requests   任何成员：该工作项的 DDL 变更历史
- GET  /deadline-change-requests?role=mine         本人：我发起的 DDL 变更申请摘要
- GET  /deadline-change-requests/{id}              任何成员：单条详情（含 reason 与 impact_analysis）
- POST /deadline-change-requests/{id}/approve      仅负责人：PENDING_APPROVAL → APPROVED（同事务更新目标 DDL）
- POST /deadline-change-requests/{id}/reject       仅负责人：PENDING_APPROVAL → REJECTED
- POST /deadline-change-requests/{id}/cancel       仅发起人：待审批 → CANCELLED

所有写接口支持 Idempotency-Key，且要求携带 version 做乐观锁（17.2 节）。
approve/reject 可携带 decision_note（审批意见，只入审计不进通知，16 节）。
"""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.idempotency import idempotency_guard
from app.domains.deadlines.schemas import (
    DeadlineChangeCommandIn,
    DeadlineChangeCreateIn,
    DeadlineChangeDecisionIn,
    DeadlineChangeRequestOut,
    DeadlineChangeSummaryOut,
)
from app.domains.deadlines.service import (
    approve_deadline_change,
    cancel_deadline_change,
    create_deadline_change_request,
    get_detail,
    list_for_work_item,
    list_mine,
    reject_deadline_change,
)
from app.domains.project.dependencies import get_current_leader, get_current_member
from app.domains.project.models import ProjectMember
from app.infrastructure.database.engine import get_session

router = APIRouter(tags=["deadlines"])


@router.post(
    "/work-items/{item_id}/deadline-change-requests",
    response_model=DeadlineChangeRequestOut,
    status_code=201,
)
async def create_deadline_change_request_endpoint(
    item_id: uuid.UUID,
    payload: DeadlineChangeCreateIn,
    actor: ProjectMember = Depends(get_current_member),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> DeadlineChangeRequestOut:
    return await create_deadline_change_request(session, actor, item_id, payload)


@router.get(
    "/work-items/{item_id}/deadline-change-requests",
    response_model=list[DeadlineChangeSummaryOut],
)
async def list_work_item_deadline_change_requests_endpoint(
    item_id: uuid.UUID,
    _: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> list[DeadlineChangeSummaryOut]:
    return await list_for_work_item(session, item_id)


@router.get("/deadline-change-requests", response_model=list[DeadlineChangeSummaryOut])
async def list_my_deadline_change_requests_endpoint(
    role: Literal["mine"] = Query(),
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> list[DeadlineChangeSummaryOut]:
    return await list_mine(session, actor)


@router.get("/deadline-change-requests/{request_id}", response_model=DeadlineChangeRequestOut)
async def get_deadline_change_request_endpoint(
    request_id: uuid.UUID,
    _: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> DeadlineChangeRequestOut:
    return await get_detail(session, request_id)


@router.post("/deadline-change-requests/{request_id}/approve", response_model=DeadlineChangeRequestOut)
async def approve_endpoint(
    request_id: uuid.UUID,
    payload: DeadlineChangeDecisionIn,
    actor: ProjectMember = Depends(get_current_leader),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> DeadlineChangeRequestOut:
    return await approve_deadline_change(
        session, actor, request_id, payload.version, decision_note=payload.decision_note
    )


@router.post("/deadline-change-requests/{request_id}/reject", response_model=DeadlineChangeRequestOut)
async def reject_endpoint(
    request_id: uuid.UUID,
    payload: DeadlineChangeDecisionIn,
    actor: ProjectMember = Depends(get_current_leader),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> DeadlineChangeRequestOut:
    return await reject_deadline_change(
        session, actor, request_id, payload.version, decision_note=payload.decision_note
    )


@router.post("/deadline-change-requests/{request_id}/cancel", response_model=DeadlineChangeRequestOut)
async def cancel_endpoint(
    request_id: uuid.UUID,
    payload: DeadlineChangeCommandIn,
    actor: ProjectMember = Depends(get_current_member),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> DeadlineChangeRequestOut:
    return await cancel_deadline_change(session, actor, request_id, payload.version)
