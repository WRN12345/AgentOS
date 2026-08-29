"""站内通知服务。

`notify()` 只执行 `flush`，由调用方将通知和业务事实放在同一事务提交，避免业务回滚后
仍发送通知。正文只应包含摘要，不得写入审核意见等隐私内容。查询与已读仅限接收人本人，
重复标记已读保持幂等。
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
    project_id: uuid.UUID,
    recipient_id: uuid.UUID,
    type: str,
    title: str,
    body: str,
    link: str | None = None,
    outbox: list[OutgoingEvent] | None = None,
) -> Notification:
    """写入站内通知，仅执行 `flush`，由业务用例提交事务。

    `project_id` 必须由调用方从可信业务上下文派生，不能直接接受客户端输入。

    传入 `outbox` 时同步追加同内容的实时事件；调用方必须在业务 `commit` 成功后
    通过 `publish_after_commit` 发布，确保订阅者只收到已经落库的事实。
    """
    notification = Notification(
        project_id=project_id,
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
                project_id=project_id,
                recipient_id=recipient_id,
                type=type,
                title=title,
                body=body,
                link=link,
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
    project_id: uuid.UUID,
    unread_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Notification], int]:
    """按 `created_at` 倒序查询本人在当前项目的通知，并返回未读总数。"""
    stmt = (
        select(Notification)
        .where(Notification.recipient_id == recipient_id, Notification.project_id == project_id)
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
            .where(
                Notification.recipient_id == recipient_id,
                Notification.project_id == project_id,
                Notification.is_read.is_(False),
            )
        )
    ).scalar_one()
    return items, unread_count


async def mark_read(
    session: AsyncSession,
    recipient_id: uuid.UUID,
    notification_id: uuid.UUID,
    *,
    project_id: uuid.UUID,
) -> Notification:
    """幂等标记本人在当前项目的通知为已读。

    他人或其他项目的通知按不存在处理，避免泄露其存在性。
    """
    notification = await session.get(Notification, notification_id)
    if (
        notification is None
        or notification.recipient_id != recipient_id
        or notification.project_id != project_id
    ):
        raise ApiException(404, ErrorCodes.NOT_FOUND, "通知不存在")
    if not notification.is_read:
        notification.is_read = True
        notification.read_at = datetime.now(UTC)
        await session.flush()
        await session.commit()
        # 异步会话不能隐式重载数据库更新后过期的 `updated_at`
        await session.refresh(notification)
    return notification
