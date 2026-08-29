"""到期和逾期提醒扫描测试。

覆盖：
- 24h 内到期未终态工作项/协作请求 → reminder.due_soon 通知 + SSE 事件；
- 已逾期未终态 → reminder.overdue；
- 终态对象不提醒；
- 去重：同一对象同一类提醒每个自然日只发一次（重复扫描不重复通知）；
- worker handle_task 对 due.scan 的分发。
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.domains.collaboration.models import CollaborationRequest
from app.domains.notifications.models import Notification
from app.domains.project.models import Project, ProjectMember
from app.domains.work_items.models import WorkItem
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.events import channel_for
from app.workers.due_scan import scan_due_reminders
from app.workers.worker import handle_task
from tests.conftest import add_member


async def _make_work_item(
    assignee_id: uuid.UUID, *, project_id: uuid.UUID, status: str, due_at: datetime | None
) -> WorkItem:
    async with async_session_factory() as session:
        item = WorkItem(
            title="临期工作项",
            description="",
            project_id=project_id,
            assignee_id=assignee_id,
            status=status,
            due_at=due_at,
        )
        item.collaborators = []
        session.add(item)
        await session.commit()
        return item


async def _make_collab(
    work_item_id: uuid.UUID,
    requester_id: uuid.UUID,
    assignee_id: uuid.UUID,
    *,
    status: str,
    due_at: datetime | None,
) -> CollaborationRequest:
    async with async_session_factory() as session:
        collab = CollaborationRequest(
            work_item_id=work_item_id,
            requester_id=requester_id,
            assignee_id=assignee_id,
            title="临期协作",
            goal="目标",
            status=status,
            due_at=due_at,
        )
        session.add(collab)
        await session.commit()
        return collab


async def _notifications_of(member_id: uuid.UUID) -> list[Notification]:
    async with async_session_factory() as session:
        return list(
            (
                await session.execute(
                    select(Notification)
                    .where(Notification.recipient_id == member_id)
                    .order_by(Notification.created_at)
                )
            )
            .scalars()
            .all()
        )


async def _count_notifications() -> int:
    async with async_session_factory() as session:
        return (
            await session.execute(select(func.count()).select_from(Notification))
        ).scalar_one()


async def test_due_scan_sends_and_deduplicates(project: Project) -> None:
    """临期工作项 + 临期协作 → 各发一条 reminder.due_soon；重复扫描不重复通知。"""
    _, alice = await add_member(project, "alice", "Alice123!", display_name="爱丽丝")
    _, bob = await add_member(project, "bob", "Bob123!", display_name="鲍勃")
    now = datetime.now(UTC)
    item = await _make_work_item(
        alice.id, project_id=project.id, status="READY", due_at=now + timedelta(hours=1)
    )
    collab = await _make_collab(
        item.id, alice.id, bob.id, status="IN_PROGRESS", due_at=now + timedelta(hours=2)
    )

    redis_client = create_redis_client()
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel_for(alice.id), channel_for(bob.id))
    try:
        stats = await scan_due_reminders(redis_client)
        assert stats == {"sent": 2, "skipped": 0}

        alice_notices = await _notifications_of(alice.id)
        assert [n.type for n in alice_notices] == ["reminder.due_soon"]
        assert "临期工作项" in alice_notices[0].body
        bob_notices = await _notifications_of(bob.id)
        assert [n.type for n in bob_notices] == ["reminder.due_soon"]

        # SSE 事件同步发布到各自频道
        received: dict[str, dict] = {}
        async with asyncio.timeout(5):
            while len(received) < 2:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message and message.get("type") == "message":
                    received[message["channel"]] = json.loads(message["data"])
        assert received[channel_for(alice.id)]["type"] == "reminder.due_soon"
        assert received[channel_for(bob.id)]["type"] == "reminder.due_soon"

        stats = await scan_due_reminders(redis_client)
        assert stats == {"sent": 0, "skipped": 2}
        assert await _count_notifications() == 2
    finally:
        await pubsub.aclose()
        await redis_client.aclose()


async def test_due_scan_overdue_and_terminal_skipped(project: Project) -> None:
    """逾期 → reminder.overdue；终态（COMPLETED / CANCELLED）对象不提醒。"""
    _, alice = await add_member(project, "alice", "Alice123!", display_name="爱丽丝")
    _, bob = await add_member(project, "bob", "Bob123!", display_name="鲍勃")
    now = datetime.now(UTC)
    overdue_item = await _make_work_item(
        alice.id, project_id=project.id, status="IN_PROGRESS", due_at=now - timedelta(hours=1)
    )
    await _make_work_item(
        alice.id, project_id=project.id, status="COMPLETED", due_at=now + timedelta(hours=1)
    )
    await _make_collab(
        overdue_item.id, alice.id, bob.id, status="CANCELLED", due_at=now + timedelta(hours=1)
    )

    redis_client = create_redis_client()
    try:
        stats = await scan_due_reminders(redis_client)
        assert stats == {"sent": 1, "skipped": 0}
        notices = await _notifications_of(alice.id)
        assert [n.type for n in notices] == ["reminder.overdue"]
        assert await _notifications_of(bob.id) == []
    finally:
        await redis_client.aclose()


async def test_worker_handle_task_dispatches_due_scan(project: Project) -> None:
    """worker handle_task 认识 due.scan 类型并执行扫描。"""
    _, alice = await add_member(project, "alice", "Alice123!", display_name="爱丽丝")
    now = datetime.now(UTC)
    await _make_work_item(
        alice.id, project_id=project.id, status="READY", due_at=now + timedelta(hours=3)
    )

    redis_client = create_redis_client()
    try:
        await handle_task({"id": str(uuid.uuid4()), "type": "due.scan", "payload": {}}, redis_client)
        notices = await _notifications_of(alice.id)
        assert [n.type for n in notices] == ["reminder.due_soon"]
    finally:
        await redis_client.aclose()
