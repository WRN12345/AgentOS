"""开发文档接口（设计文档 2026-07-30 §4.2）。

- GET  /work-items/{id}/dev-doc          任何项目成员：查看文档（含最近 AI 初审建议关联）
- PUT  /work-items/{id}/dev-doc          仅主执行人：撰写/编辑草稿（upsert，乐观锁）
- POST /work-items/{id}/dev-doc/submit   仅主执行人：提交审核（触发 Agent 初审）
- POST /work-items/{id}/dev-doc/confirm  仅负责人：确认通过
- POST /work-items/{id}/dev-doc/return   仅负责人：打回（附理由）
- POST /work-items/{id}/dev-doc/waive    仅负责人：豁免该任务文档要求（写审计）

所有写接口支持 Idempotency-Key，命令接口携带 version 做乐观锁（17.2 节）。
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
    _: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> DevDocOut:
    return await get_dev_doc_for_work_item(session, item_id)


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
