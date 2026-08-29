"""审计记录只读查询接口 `GET /audit-events`。

项目负责人只能查看本项目事件；全局管理员可只读查看所有项目事件及
`project_id` 为 `NULL` 的全局事件。
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
    """按创建时间倒序返回当前身份可见的审计事件。"""
    stmt = (
        select(AuditEvent)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if not current_user.is_admin:
        # 使用中间件保存且已经门禁校验的 `X-Project-Id` 快照，避免重复解析请求头产生偏差。
        stmt = stmt.where(AuditEvent.project_id == get_project_id())
    return list((await session.execute(stmt)).scalars().all())
