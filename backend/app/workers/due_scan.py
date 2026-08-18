"""到期/逾期提醒扫描（4.2、4.3 节，T3.6）。

由 scheduler 周期 enqueue `due.scan`、worker 消费执行：

- 扫描窗口内（默认未来 24h）到期且未终态的工作项与协作请求 → `reminder.due_soon`；
- 已逾期且未终态的 → `reminder.overdue`；
- 接收人：工作项主执行人 / 协作请求接收人；
- 出口只有 notifications + SSE 事件，绝不触碰任何业务状态（4.2 硬约束）。

去重：同一对象同一类提醒每个自然日只发一次——Redis 键
`agentos:reminded:{type}:{obj_id}:{date}` SET NX EX 86400，
抢到键才写通知，简单可靠（重复扫描/多实例并发都不会重复提醒）。
"""

import uuid
from datetime import UTC, datetime, timedelta

import redis.asyncio as redis
from sqlalchemy import select

from app.core.config import settings
from app.core.logging import setup_logging
from app.domains.collaboration.models import CollaborationRequest
from app.domains.collaboration.state_machine import CollaborationStatus
from app.domains.notifications.service import notify
from app.domains.work_items.models import WorkItem
from app.domains.work_items.state_machine import WorkItemStatus
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.events import OutgoingEvent, publish_events

logger = setup_logging("worker")

# 未终态才提醒：已完成/已取消（含协作的被拒绝）不再打扰
_WORK_ITEM_TERMINAL = (WorkItemStatus.COMPLETED.value, WorkItemStatus.CANCELLED.value)
_COLLAB_TERMINAL = (
    CollaborationStatus.DECLINED.value,
    CollaborationStatus.CANCELLED.value,
    CollaborationStatus.COMPLETED.value,
)

REMINDER_DUE_SOON = "reminder.due_soon"
REMINDER_OVERDUE = "reminder.overdue"


async def _mark_reminded(
    client: redis.Redis, reminder_type: str, obj_id: uuid.UUID, today: str
) -> bool:
    """SETNX 抢占当日提醒名额：返回 True 表示本次应发提醒。"""
    key = f"agentos:reminded:{reminder_type}:{obj_id}:{today}"
    return bool(await client.set(key, "1", nx=True, ex=86400))


def _reminder_type(due_at: datetime, now: datetime) -> str:
    return REMINDER_OVERDUE if due_at < now else REMINDER_DUE_SOON


def _fmt_due(due_at: datetime) -> str:
    return due_at.strftime("%Y-%m-%d %H:%M")


async def scan_due_reminders(client: redis.Redis) -> dict[str, int]:
    """执行一轮扫描，返回统计（sent/skipped）。"""
    now = datetime.now(UTC)
    horizon = now + timedelta(hours=settings.due_soon_horizon_hours)
    today = now.date().isoformat()
    events: list[OutgoingEvent] = []
    stats = {"sent": 0, "skipped": 0}

    async with async_session_factory() as session:
        work_items = list(
            (
                await session.execute(
                    select(WorkItem).where(
                        WorkItem.due_at.is_not(None),
                        WorkItem.due_at <= horizon,
                        WorkItem.status.notin_(_WORK_ITEM_TERMINAL),
                    )
                )
            )
            .scalars()
            .all()
        )
        for item in work_items:
            assert item.due_at is not None
            reminder_type = _reminder_type(item.due_at, now)
            if not await _mark_reminded(client, reminder_type, item.id, today):
                stats["skipped"] += 1
                continue
            if reminder_type == REMINDER_OVERDUE:
                title, body = (
                    "工作项已逾期",
                    f"工作项「{item.title}」已超过截止时间 {_fmt_due(item.due_at)}，请尽快处理",
                )
            else:
                title, body = (
                    "工作项即将到期",
                    f"工作项「{item.title}」将于 {_fmt_due(item.due_at)} 到期",
                )
            await notify(
                session,
                project_id=item.project_id,
                recipient_id=item.assignee_id,
                type=reminder_type,
                title=title,
                body=body,
                link=f"/work-items/{item.id}",
                outbox=events,
            )
            stats["sent"] += 1

        collabs = list(
            (
                await session.execute(
                    select(CollaborationRequest).where(
                        CollaborationRequest.due_at.is_not(None),
                        CollaborationRequest.due_at <= horizon,
                        CollaborationRequest.status.notin_(_COLLAB_TERMINAL),
                    )
                )
            )
            .scalars()
            .all()
        )
        for collab in collabs:
            assert collab.due_at is not None
            # 项目归属经关联工作项推导（collab 无 project_id 冗余列，spec D1）
            work_item = await session.get(WorkItem, collab.work_item_id)
            if work_item is None:
                stats["skipped"] += 1
                continue
            reminder_type = _reminder_type(collab.due_at, now)
            if not await _mark_reminded(client, reminder_type, collab.id, today):
                stats["skipped"] += 1
                continue
            if reminder_type == REMINDER_OVERDUE:
                title, body = (
                    "协作请求已逾期",
                    f"协作请求「{collab.title}」已超过截止时间 {_fmt_due(collab.due_at)}，请尽快回传",
                )
            else:
                title, body = (
                    "协作请求即将到期",
                    f"协作请求「{collab.title}」将于 {_fmt_due(collab.due_at)} 到期",
                )
            await notify(
                session,
                project_id=work_item.project_id,
                recipient_id=collab.assignee_id,
                type=reminder_type,
                title=title,
                body=body,
                link=f"/work-items/{collab.work_item_id}",
                outbox=events,
            )
            stats["sent"] += 1

        await session.commit()

    # 通知已落库（commit 成功）后再发 SSE 事件（4.3 节）
    await publish_events(client, events)
    logger.info("due scan finished: sent=%s skipped=%s", stats["sent"], stats["skipped"])
    return stats
