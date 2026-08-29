"""审核接口。

- POST /work-items/{id}/reviews   仅项目负责人提交审核结论
- GET  /work-items/{id}/reviews   仅负责人与该工作项主执行人查看含反馈的记录

提交接口支持 Idempotency-Key，重放不会重复推进状态或创建审核记录。
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.idempotency import idempotency_guard
from app.domains.project.dependencies import get_current_member
from app.domains.project.models import ProjectMember
from app.domains.reviews.schemas import ReviewCreateIn, ReviewOut
from app.domains.reviews.service import create_review, list_reviews
from app.infrastructure.database.engine import get_session

router = APIRouter(tags=["reviews"])


@router.post(
    "/work-items/{item_id}/reviews",
    response_model=ReviewOut,
    status_code=201,
)
async def create_review_endpoint(
    item_id: uuid.UUID,
    payload: ReviewCreateIn,
    actor: ProjectMember = Depends(get_current_member),
    _: None = Depends(idempotency_guard),
    session: AsyncSession = Depends(get_session),
) -> ReviewOut:
    return await create_review(session, actor, item_id, payload)


@router.get(
    "/work-items/{item_id}/reviews",
    response_model=list[ReviewOut],
)
async def list_reviews_endpoint(
    item_id: uuid.UUID,
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> list[ReviewOut]:
    return await list_reviews(session, actor, item_id)
