"""开发文档接口。

主执行人负责撰写和提交，负责人负责确认、打回或豁免，项目成员可查看文档。
所有写接口支持 `Idempotency-Key`，状态命令通过 `version` 实施乐观锁。
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.idempotency import idempotency_guard
from app.domains.dev_docs.schemas import (
    DevDocCommandIn,
    DevDocOut,
    DevDocReturnIn,
    DevDocUpdateIn,
    DevDocWaiveIn,
)
from app.domains.dev_docs.service import (
    confirm_dev_doc,
    get_dev_doc_for_work_item,
    return_dev_doc,
    submit_dev_doc,
    upsert_dev_doc,
    waive_dev_doc,
)
from app.domains.project.dependencies import get_current_leader, get_current_member
from app.domains.project.models import ProjectMember
from app.infrastructure.database.engine import get_session

router = APIRouter(tags=["dev_docs"])


@router.get("/work-items/{item_id}/dev-doc", response_model=DevDocOut)
async def get_dev_doc_endpoint(
    item_id: uuid.UUID,
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> DevDocOut:
    return await get_dev_doc_for_work_item(session, actor, item_id)


@router.put("/work-items/{item_id}/dev-doc", response_model=DevDocOut)
async def upsert_dev_doc_endpoint(
    item_id: uuid.UUID,
    payload: DevDocUpdateIn,
    actor: ProjectMember = Depends(get_current_member),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> DevDocOut:
    return await upsert_dev_doc(session, actor, item_id, payload)


@router.post("/work-items/{item_id}/dev-doc/submit", response_model=DevDocOut)
async def submit_dev_doc_endpoint(
    item_id: uuid.UUID,
    payload: DevDocCommandIn,
    actor: ProjectMember = Depends(get_current_member),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> DevDocOut:
    return await submit_dev_doc(session, actor, item_id, payload.version)


@router.post("/work-items/{item_id}/dev-doc/confirm", response_model=DevDocOut)
async def confirm_dev_doc_endpoint(
    item_id: uuid.UUID,
    payload: DevDocCommandIn,
    actor: ProjectMember = Depends(get_current_leader),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> DevDocOut:
    return await confirm_dev_doc(session, actor, item_id, payload.version)


@router.post("/work-items/{item_id}/dev-doc/return", response_model=DevDocOut)
async def return_dev_doc_endpoint(
    item_id: uuid.UUID,
    payload: DevDocReturnIn,
    actor: ProjectMember = Depends(get_current_leader),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> DevDocOut:
    return await return_dev_doc(
        session, actor, item_id, payload.version, payload.review_note
    )


@router.post("/work-items/{item_id}/dev-doc/waive", response_model=DevDocOut)
async def waive_dev_doc_endpoint(
    item_id: uuid.UUID,
    payload: DevDocWaiveIn,
    actor: ProjectMember = Depends(get_current_leader),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> DevDocOut:
    return await waive_dev_doc(session, actor, item_id, payload.version)
