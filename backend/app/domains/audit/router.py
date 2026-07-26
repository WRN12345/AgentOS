"""审计只读查询接口（12.6 节 GET /audit-events）。

权限：仅项目负责人可查（6.1 节"查看全部任务、协作和审计记录"，16 节）。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.audit.models import AuditEvent
from app.domains.audit.schemas import AuditEventOut
from app.domains.project.dependencies import get_current_leader
from app.domains.project.models import ProjectMember
from app.infrastructure.database.engine import get_session

router = APIRouter(prefix="/audit-events", tags=["audit"])


@router.get("", response_model=list[AuditEventOut])
async def list_audit_events(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: ProjectMember = Depends(get_current_leader),
    session: AsyncSession = Depends(get_session),
) -> list[AuditEvent]:
    """按创建时间倒序返回审计事件（只读）。"""
    stmt = (
        select(AuditEvent)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(stmt)).scalars().all())
