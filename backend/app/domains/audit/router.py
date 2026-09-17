"""审计记录只读查询接口 `GET /audit-events`。

项目负责人只能查看本项目事件；全局管理员可只读查看所有项目事件及
`project_id` 为 `NULL` 的全局事件。
"""

import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import Text, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiException, ErrorCodes
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
    project_id: uuid.UUID | None = None,
    platform_only: bool = False,
    q: str | None = Query(default=None, max_length=200),
    current_user: User = Depends(get_current_leader_or_admin),
    session: AsyncSession = Depends(get_session),
) -> list[AuditEvent]:
    """按创建时间倒序返回当前身份可见的审计事件。"""
    if project_id is not None and platform_only:
        raise ApiException(422, ErrorCodes.VALIDATION_ERROR, "project_id 与 platform_only 不能同时使用")
    stmt = select(AuditEvent)
    if not current_user.is_admin:
        # 使用中间件保存且已经门禁校验的 `X-Project-Id` 快照，避免重复解析请求头产生偏差。
        stmt = stmt.where(AuditEvent.project_id == get_project_id())
    if project_id is not None:
        stmt = stmt.where(AuditEvent.project_id == project_id)
    if platform_only:
        stmt = stmt.where(AuditEvent.project_id.is_(None))
    if q and (keyword := q.strip()):
        stmt = stmt.outerjoin(User, User.id == AuditEvent.actor_id).where(or_(
            *(column.icontains(keyword, autoescape=True) for column in (
                User.username,
                AuditEvent.action,
                AuditEvent.target_type,
                cast(AuditEvent.target_id, Text),
                cast(AuditEvent.before, Text),
                cast(AuditEvent.after, Text),
            ))
        ))
    stmt = (
        stmt.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(stmt)).scalars().all())
