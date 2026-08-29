"""DDL 变更申请接口。

主任务级申请由主执行人或负责人发起，协作级申请由协作双方发起；审批命令仅负责人
可执行，取消命令仅申请人可执行。
所有写接口支持 `Idempotency-Key`，状态命令通过 `version` 实施乐观锁。
`approve` 和 `reject` 的 `decision_note` 仅进入审计记录，不进入通知正文。
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
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> list[DeadlineChangeSummaryOut]:
    return await list_for_work_item(session, item_id, project_id=actor.project_id)


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
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> DeadlineChangeRequestOut:
    return await get_detail(session, request_id, project_id=actor.project_id)


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
