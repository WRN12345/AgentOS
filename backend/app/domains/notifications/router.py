"""当前项目的站内通知接口。

通知列表和已读操作仅限接收人本人；重复标记已读仍返回成功。
"""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.notifications.schemas import NotificationListOut, NotificationOut
from app.domains.notifications.service import list_mine, mark_read, to_out
from app.domains.project.dependencies import get_current_member
from app.domains.project.models import ProjectMember
from app.infrastructure.database.engine import get_session

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=NotificationListOut)
async def list_notifications_endpoint(
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> NotificationListOut:
    items, unread_count = await list_mine(
        session,
        actor.id,
        project_id=actor.project_id,
        unread_only=unread_only,
        limit=limit,
        offset=offset,
    )
    return NotificationListOut(items=[to_out(n) for n in items], unread_count=unread_count)


@router.post("/{notification_id}/read", response_model=NotificationOut)
async def mark_read_endpoint(
    notification_id: uuid.UUID,
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> NotificationOut:
    return to_out(await mark_read(session, actor.id, notification_id, project_id=actor.project_id))
