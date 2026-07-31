"""交付物接口（12.5 节）。

- POST /work-items/{id}/deliverables             仅当前主执行人：提交新版本（三类）
- GET  /work-items/{id}/deliverables             负责人/工作项相关成员：版本历史
- GET  /work-items/{id}/deliverables/{version}   负责人/工作项相关成员：按版本查询
- GET  /deliverables                            聚合页：负责人/管理员见全部，成员见相关工作项
- GET  /deliverables?role=mine                  本人：我提交的交付物及审核结论

提交接口支持 Idempotency-Key；版本号由服务端递增，并发冲突返回 409（17.2 节）。
"""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.idempotency import idempotency_guard
from app.domains.deliverables.schemas import (
    DeliverableCreateIn,
    DeliverableListItemOut,
    DeliverableOut,
)
from app.domains.deliverables.service import (
    create_deliverable,
    get_deliverable_version,
    list_deliverables,
    list_mine,
    list_visible,
)
from app.domains.project.dependencies import get_current_member
from app.domains.project.models import ProjectMember
from app.infrastructure.database.engine import get_session

router = APIRouter(tags=["deliverables"])


@router.post(
    "/work-items/{item_id}/deliverables",
    response_model=DeliverableOut,
    status_code=201,
)
async def create_deliverable_endpoint(
    item_id: uuid.UUID,
    payload: DeliverableCreateIn,
    actor: ProjectMember = Depends(get_current_member),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> DeliverableOut:
    return await create_deliverable(session, actor, item_id, payload)


@router.get(
    "/work-items/{item_id}/deliverables",
    response_model=list[DeliverableOut],
)
async def list_deliverables_endpoint(
    item_id: uuid.UUID,
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> list[DeliverableOut]:
    return await list_deliverables(session, actor, item_id)


@router.get("/deliverables", response_model=list[DeliverableListItemOut])
async def list_deliverables_aggregate_endpoint(
    role: Literal["mine", "visible"] = Query("visible"),
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> list[DeliverableListItemOut]:
    """交付物聚合列表：默认按可见范围（负责人/管理员全部，成员相关工作项）；
    role=mine 仅我提交的（审批中心"我的申请"页）。"""
    if role == "mine":
        return await list_mine(session, actor)
    return await list_visible(session, actor)


@router.get(
    "/work-items/{item_id}/deliverables/{version}",
    response_model=DeliverableOut,
)
async def get_deliverable_version_endpoint(
    item_id: uuid.UUID,
    version: int,
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> DeliverableOut:
    return await get_deliverable_version(session, actor, item_id, version)
