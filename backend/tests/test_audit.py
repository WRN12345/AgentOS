"""审计基础设施测试（T2.4 验收，原则 5 与第 16 章）。"""

import uuid

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
from tests.conftest import (
    add_member,
    add_member_for_existing_user,
    auth_headers,
    create_admin_user,
)


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
        # 无项目上下文（全局接口/直接写入）时 project_id 记为 NULL
        assert saved.project_id is None
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
    # 事件落库时从请求上下文快照项目归属（ticket 07）
    set_request_context("req-audit-0002", "10.0.0.2", project_id=project.id)
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
    # 负责人只见本项目事件，事件带项目归属
    assert item["project_id"] == str(project.id)
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


# ---------- ticket 07：审计事件项目归属 ----------


async def test_audit_event_captures_project_from_request_context(
    project_a: Project,
) -> None:
    """ticket 07：record_event 从请求上下文快照 project_id；无上下文时记为 NULL。"""
    set_request_context("req-audit-0003", "10.0.0.3", project_id=project_a.id)
    try:
        async with async_session_factory() as session:
            user = await create_user(session, "dora", "Password1!")
            event = await record_event(
                session,
                actor_id=user.id,
                action="user.create",
                target_type="user",
                target_id=user.id,
            )
            await session.commit()
            event_id = event.id

        async with async_session_factory() as session:
            saved = await session.get(AuditEvent, event_id)
        assert saved.project_id == project_a.id
    finally:
        set_request_context("", "")

    # 无项目上下文的全局事件：project_id 为 NULL（全局动作零值桶）
    async with async_session_factory() as session:
        event = await record_event(session, action="admin.global")
        await session.commit()
        global_event = await session.get(AuditEvent, event.id)
    assert global_event.project_id is None


async def test_audit_events_project_scoped_for_leaders_admin_sees_all(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """ticket 07 验收：负责人只见本项目事件，admin 见全局，事件带项目归属。

    载体：同一 leader 账号挂 A/B 双项目（均负责人），各创建一工作项产生审计事件。
    """
    async with async_session_factory() as session:
        leader = await create_user(session, "leader", "Leader123!")
        await session.commit()
    await add_member_for_existing_user(
        async_session_factory, project_a, leader, role="leader", display_name="负责人"
    )
    await add_member_for_existing_user(
        async_session_factory, project_b, leader, role="leader", display_name="负责人"
    )
    _, alice = await add_member(project_a, "alice", "Alice123!")
    _, bob = await add_member(project_b, "bob", "Bob123!")
    await create_admin_user()

    headers_a = await auth_headers(client, "leader", "Leader123!", project_id=str(project_a.id))
    headers_b = await auth_headers(client, "leader", "Leader123!", project_id=str(project_b.id))
    admin_headers = await auth_headers(client, "admin", "Admin123!")

    # A/B 各创建一工作项，产生分属两项目的事件
    item_a = await client.post(
        "/api/v1/work-items",
        json={"title": "A 项目事件", "assignee_id": str(alice.id)},
        headers=headers_a,
    )
    assert item_a.status_code == 201, item_a.text
    item_b = await client.post(
        "/api/v1/work-items",
        json={"title": "B 项目事件", "assignee_id": str(bob.id)},
        headers=headers_b,
    )
    assert item_b.status_code == 201, item_b.text

    # 负责人 A：只见 A 项目事件（响应含 project_id），看不到 B
    resp_a = await client.get("/api/v1/audit-events", headers=headers_a)
    assert resp_a.status_code == 200
    items_a = resp_a.json()
    assert items_a
    assert all(item["project_id"] == str(project_a.id) for item in items_a)
    a_ids = {item["target_id"] for item in items_a}
    assert item_a.json()["id"] in a_ids
    assert item_b.json()["id"] not in a_ids

    # 负责人 B：只见 B 项目事件
    resp_b = await client.get("/api/v1/audit-events", headers=headers_b)
    assert resp_b.status_code == 200
    items_b = resp_b.json()
    assert items_b
    assert all(item["project_id"] == str(project_b.id) for item in items_b)
    b_ids = {item["target_id"] for item in items_b}
    assert item_b.json()["id"] in b_ids
    assert item_a.json()["id"] not in b_ids

    # admin 无成员记录仍可访问（get_current_leader_or_admin 放行），且见全局
    resp_admin = await client.get("/api/v1/audit-events", headers=admin_headers)
    assert resp_admin.status_code == 200
    all_ids = {item["target_id"] for item in resp_admin.json()}
    assert item_a.json()["id"] in all_ids
    assert item_b.json()["id"] in all_ids


async def test_project_scoped_write_never_buckets_event_as_global(
    client: httpx.AsyncClient, project_a: Project
) -> None:
    """ticket 07 守卫：项目动作经 HTTP 落库必带 project_id，绝不静默归为全局（NULL）。

    D1 取舍下 project_id 可空，风险是「代码路径漏设项目上下文 → 事件被静默当成
    全局动作、负责人视图不可见」。本测试钉住不变量：HTTP 流中同一 X-Project-Id 头
    既门禁写操作（get_current_member 缺失→400），又供 RequestContextMiddleware
    快照，因此项目动作的审计事件必非 NULL；未来若出现直接落 NULL 的路径，此测试失败。
    """
    _, guard = await add_member(
        project_a, "guard", "Guard123!", role="leader", display_name="守卫"
    )
    _, assignee = await add_member(
        project_a, "guard_a2", "GuardA2!", display_name="被指派"
    )
    headers = await auth_headers(client, "guard", "Guard123!", project_id=str(project_a.id))

    resp = await client.post(
        "/api/v1/work-items",
        json={"title": "守卫：项目事件必须带项目归属", "assignee_id": str(assignee.id)},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    target_id = uuid.UUID(resp.json()["id"])

    async with async_session_factory() as session:
        rows = (
            await session.execute(select(AuditEvent).where(AuditEvent.target_id == target_id))
        ).scalars().all()
    assert rows, "工作项创建应产生审计事件"
    assert all(row.project_id == project_a.id for row in rows), (
        "项目动作的审计事件必须携带 project_id；出现 NULL 说明上下文快照缺失"
    )
