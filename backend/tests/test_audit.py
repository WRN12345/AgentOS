"""审计基础设施测试（T2.4 验收，原则 5 与第 16 章）。"""

import httpx
import pytest
from sqlalchemy import select

from app.core.request_context import set_request_context
from app.domains.audit.models import AuditEvent
from app.domains.audit.service import record_event
from app.domains.identity.models import User
from app.domains.identity.service import create_user
from app.domains.project.models import Project
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member, auth_headers


async def test_record_event_contains_all_required_fields() -> None:
    """事件包含操作者、动作、目标、前后摘要、request_id、来源 IP、时间全部字段。"""
    set_request_context("req-audit-0001", "10.0.0.1")
    try:
        async with async_session_factory() as session:
            user = await create_user(session, "bob", "Password1!")
            event = await record_event(
                session,
                actor_id=user.id,
                action="user.create",
                target_type="user",
                target_id=user.id,
                before=None,
                after={"username": "bob"},
            )
            await session.commit()
            event_id = event.id

        async with async_session_factory() as session:
            saved = await session.get(AuditEvent, event_id)
        assert saved.actor_id == user.id
        assert saved.action == "user.create"
        assert saved.target_type == "user"
        assert saved.target_id == user.id
        assert saved.before is None
        assert saved.after == {"username": "bob"}
        assert saved.request_id == "req-audit-0001"
        assert saved.source_ip == "10.0.0.1"
        assert saved.created_at is not None
    finally:
        set_request_context("", "")


async def test_event_failure_rolls_back_business_write() -> None:
    """事件写入失败时业务写入一并回滚（同一事务，同生共死）。"""
    with pytest.raises(Exception):  # action=None 违反 NOT NULL，flush 即抛错
        async with async_session_factory() as session:
            await create_user(session, "carol", "Password1!")
            await record_event(session, action=None)  # type: ignore[arg-type]
            await session.commit()

    async with async_session_factory() as session:
        user = (
            await session.execute(select(User).where(User.username == "carol"))
        ).scalar_one_or_none()
        events = (await session.execute(select(AuditEvent))).scalars().all()
    assert user is None
    assert events == []


async def test_list_audit_events_permission(client: httpx.AsyncClient, project: Project) -> None:
    """GET /audit-events：仅项目负责人可查（T2.3 收紧）；普通成员 403，未登录 401。"""
    set_request_context("req-audit-0002", "10.0.0.2")
    async with async_session_factory() as session:
        user, _ = await add_member(project, "dave", "Password1!", role="leader")
        await record_event(session, actor_id=user.id, action="user.create", target_type="user")
        await session.commit()
    set_request_context("", "")
    # 普通成员（无负责人角色）→ 403
    await add_member(project, "erin", "Password1!")

    leader_headers = await auth_headers(client, "dave", "Password1!", project_id=str(project.id))
    resp = await client.get("/api/v1/audit-events", headers=leader_headers)
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    item = items[0]
    assert item["action"] == "user.create"
    assert item["actor_id"] == str(user.id)
    assert item["target_type"] == "user"
    assert item["request_id"] == "req-audit-0002"
    assert item["source_ip"] == "10.0.0.2"
    assert item["created_at"]

    member_headers = await auth_headers(client, "erin", "Password1!", project_id=str(project.id))
    forbidden = await client.get("/api/v1/audit-events", headers=member_headers)
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "FORBIDDEN"

    anon = await client.get("/api/v1/audit-events")
    assert anon.status_code == 401
