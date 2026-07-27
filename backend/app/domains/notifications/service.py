"""站内通知服务（2.1、12.6、16 节）。

- notify() 供各业务模块在关键事件时调用：与业务写入同一事务，只 flush 不 commit，
  保证通知不丢（业务回滚则通知一并回滚，不会出现"通知到了但事没成"）；
- 正文只含摘要信息，不写入审核意见等隐私内容（16 节）；
- 查询与已读仅限本人；已读操作天然幂等（重复已读返回成功，不报错）。
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiException, ErrorCodes
from app.domains.notifications.models import Notification
from app.domains.notifications.schemas import NotificationOut
from app.infrastructure.events import OutgoingEvent


async def notify(
    session: AsyncSession,
    *,
    recipient_id: uuid.UUID,
    type: str,
    title: str,
    body: str,
    link: str | None = None,
    outbox: list[OutgoingEvent] | None = None,
) -> Notification:
    """写入一条站内通知。只 flush 不 commit，由业务用例统一提交。

    传入 outbox 时同步追加一条同内容实时事件；调用方须在业务 commit
    成功后发布 outbox（infrastructure/events.publish_after_commit），
    保证订阅者收到的事件对应已落库事实（4.3 节）。
    """
    notification = Notification(
        recipient_id=recipient_id,
        type=type,
        title=title,
        body=body,
        link=link,
    )
    session.add(notification)
    await session.flush()
    if outbox is not None:
        outbox.append(
            OutgoingEvent(
                recipient_id=recipient_id, type=type, title=title, body=body, link=link
            )
        )
    return notification


def to_out(notification: Notification) -> NotificationOut:
    return NotificationOut(
        id=notification.id,
        type=notification.type,
        title=notification.title,
        body=notification.body,
        link=notification.link,
        is_read=notification.is_read,
        read_at=notification.read_at,
        created_at=notification.created_at,
    )


async def list_mine(
    session: AsyncSession,
    recipient_id: uuid.UUID,
    *,
    unread_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Notification], int]:
    """查询本人通知（created_at 倒序），并返回当前未读总数。"""
    stmt = (
        select(Notification)
        .where(Notification.recipient_id == recipient_id)
        .order_by(Notification.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if unread_only:
        stmt = stmt.where(Notification.is_read.is_(False))
    items = list((await session.execute(stmt)).scalars().all())

    unread_count = (
        await session.execute(
            select(func.count())
            .select_from(Notification)
            .where(Notification.recipient_id == recipient_id, Notification.is_read.is_(False))
        )
    ).scalar_one()
    return items, unread_count


async def mark_read(
    session: AsyncSession, recipient_id: uuid.UUID, notification_id: uuid.UUID
) -> Notification:
    """标记已读（仅本人；他人通知按 404 处理，不暴露存在性）。幂等：重复已读直接返回。"""
    notification = await session.get(Notification, notification_id)
    if notification is None or notification.recipient_id != recipient_id:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "通知不存在")
    if not notification.is_read:
        notification.is_read = True
        notification.read_at = datetime.now(UTC)
        await session.flush()
        await session.commit()
        await session.refresh(notification)  # updated_at 由数据库 onupdate 生成
    return notification
