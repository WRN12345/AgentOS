"""负责人待审批聚合接口（12.6 节）。

GET /approvals：负责人返回 PENDING 转派申请 + PENDING_APPROVAL DDL 变更申请
的统一列表；普通成员返回空列表（不 403，T3.5 验收）。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.approvals.schemas import ApprovalItemOut
from app.domains.approvals.service import list_pending_approvals
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
