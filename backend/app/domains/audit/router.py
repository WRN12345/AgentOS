"""审计只读查询接口（12.6 节 GET /audit-events）。

权限：项目负责人与全局管理员可查（6.1 节"查看全部任务、协作和审计记录"，16 节；
管理员为只读管理视图，不做业务写操作）。

项目归属（spec D1 / ticket 07）：事件带 project_id；查询按身份分流——
全局管理员可见全部项目事件（管理控制台视图）；项目负责人仅可见本项目事件
（多项目隔离：墙外事件等同不存在）。全局事件（project_id NULL）仅管理员可见。
"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.request_context import get_project_id
from app.domains.audit.models import AuditEvent
from app.domains.audit.schemas import AuditEventOut
from app.domains.identity.models import User
from app.domains.project.dependencies import get_current_leader_or_admin
from app.infrastructure.database.engine import get_session

router = APIRouter(prefix="/audit-events", tags=["audit"])


@router.get("", response_model=list[AuditEventOut])
async def list_audit_events(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_leader_or_admin),
    session: AsyncSession = Depends(get_session),
) -> list[AuditEvent]:
    """按创建时间倒序返回审计事件（只读）。

    admin 见全部项目事件；负责人仅见本项目事件（ticket 07 隔离）。
    """
    stmt = (
        select(AuditEvent)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if not current_user.is_admin:
        # 非管理员（负责人）：只可见本项目事件。get_project_id() 取中间件在请求进入时
        # 对 X-Project-Id 的快照，即 get_current_leader_or_admin 已校验过的同一项目，
        # 无需重新解析 header（避免双数据源漂移）。
        stmt = stmt.where(AuditEvent.project_id == get_project_id())
    return list((await session.execute(stmt)).scalars().all())
