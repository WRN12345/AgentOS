"""负责人审批聚合接口。

`GET /approvals` 返回待审批事项，`GET /approvals/processed` 返回最近处理记录。
普通成员访问时返回空列表而非 `403`。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.approvals.schemas import ApprovalItemOut
from app.domains.approvals.service import list_pending_approvals, list_processed_approvals
from app.domains.project.dependencies import get_current_member
from app.domains.project.models import ProjectMember
from app.infrastructure.database.engine import get_session

router = APIRouter(tags=["approvals"])


@router.get("/approvals", response_model=list[ApprovalItemOut])
async def list_approvals_endpoint(
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> list[ApprovalItemOut]:
    return await list_pending_approvals(session, actor)


@router.get("/approvals/processed", response_model=list[ApprovalItemOut])
async def list_processed_approvals_endpoint(
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> list[ApprovalItemOut]:
    """返回包含 `approved_by` 和 `approved_at` 的已处理记录。"""
    return await list_processed_approvals(session, actor)
