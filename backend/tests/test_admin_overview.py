"""管理员跨项目只读统计、关注任务与审计过滤。"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, update

from app.domains.admin import service
from app.domains.audit.models import AuditEvent
from app.domains.audit.service import record_event
from app.domains.identity.models import User
from app.domains.memory.member_stats import member_completion_stats
from app.domains.project.models import Project, ProjectMember
from app.domains.work_items.models import WorkItem, WorkItemCollaborator
from app.infrastructure.database.engine import async_session_factory, engine
from tests.conftest import add_member, add_member_for_existing_user, auth_headers


@pytest.fixture
def as_of(monkeypatch):
    now = datetime.now(UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(service, "datetime", Clock)
    return now


async def test_overview_statistics(client, admin_headers, project_a, project_b, as_of):
    user, member_a = await add_member(project_a, "alice", "Alice123!", role="leader")
    member_b = await add_member_for_existing_user(
        async_session_factory, project_b, user, display_name="Alice B"
    )
    _, zero_member = await add_member(project_a, "zero", "Zero12345!")
    async with async_session_factory() as session:
        empty = Project(name="Empty")
        session.add(empty)
        # 同一账号的成员停用和账号停用分别保留。
        await session.execute(update(ProjectMember).where(
            ProjectMember.id == member_a.id
        ).values(is_active=False))
        await session.execute(update(User).where(User.id == user.id).values(is_active=False))
        for status, due_at in [
            ("DRAFT", as_of - timedelta(days=2)),
            ("CANCELLED", as_of - timedelta(days=2)),
            ("READY", as_of - timedelta(minutes=1)),
            ("IN_PROGRESS", as_of),
            ("BLOCKED", as_of - timedelta(hours=1)),
            ("IN_REVIEW", None),
        ]:
            session.add(WorkItem(project_id=project_a.id, assignee_id=member_a.id,
                                 title=status, status=status, due_at=due_at))
        for updated_at, due_at in [
            (as_of, None),
            (as_of, as_of - timedelta(hours=1)),
            (as_of - timedelta(days=30), None),
            (as_of - timedelta(days=31), None),
            (as_of, as_of),
        ]:
            session.add(WorkItem(project_id=project_a.id, assignee_id=member_a.id,
                                 title="Completed", status="COMPLETED",
                                 updated_at=updated_at, due_at=due_at))
        session.add(WorkItem(project_id=project_b.id, assignee_id=member_b.id,
                             title="Other project", status="COMPLETED"))
        await session.commit()

    response = await client.get("/api/v1/admin/overview", headers=admin_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"as_of", "projects", "members"}
    assert datetime.fromisoformat(body["as_of"]) == as_of
    projects = {p["id"]: p for p in body["projects"]}
    a = projects[str(project_a.id)]
    assert {k: a[k] for k in ("total", "completed", "active", "overdue", "blocked")} == {
        "total": 9, "completed": 5, "active": 4, "overdue": 2, "blocked": 1,
    }
    assert a["leader"]["id"] == str(member_a.id)
    assert projects[str(project_b.id)]["total"] == 1
    assert projects[str(empty.id)]["leader"] is None
    assert all(projects[str(empty.id)][field] == 0 for field in (
        "total", "completed", "active", "overdue", "blocked"
    ))
    members = {m["member_id"]: m for m in body["members"]}
    assert members[str(member_a.id)] == {
        "member_id": str(member_a.id), "user_id": str(user.id),
        "project_id": str(project_a.id), "username": "alice", "display_name": "alice",
        "role": "leader", "is_active": False, "user_is_active": False,
        "active": 4, "completed_total": 5, "completed_recent": 4,
        "overdue": 2, "blocked": 1, "on_time_rate": 0.8, "sample_sufficient": True,
    }
    b = members[str(member_b.id)]
    assert b["is_active"] is True and b["user_is_active"] is False
    assert b["completed_total"] == 1 and b["active"] == 0
    assert b["sample_sufficient"] is False and b["on_time_rate"] == 1.0
    zero = members[str(zero_member.id)]
    assert zero["completed_total"] == zero["active"] == 0
    assert zero["on_time_rate"] is None and zero["sample_sufficient"] is False


async def test_overview_matches_member_stats(client, admin_headers, project_a, as_of):
    _, member = await add_member(project_a, "member", "Member123!")
    async with async_session_factory() as session:
        for status, due_at, updated_at in [
            ("COMPLETED", None, as_of),
            ("COMPLETED", as_of - timedelta(days=2), as_of),
            ("COMPLETED", None, as_of - timedelta(days=40)),
            ("IN_REVIEW", None, as_of),
        ]:
            session.add(WorkItem(project_id=project_a.id, assignee_id=member.id,
                                 title=status, status=status, due_at=due_at,
                                 updated_at=updated_at))
        await session.commit()
        baseline = (await member_completion_stats(session, project_id=project_a.id))[0]
    response = await client.get("/api/v1/admin/overview", headers=admin_headers)
    actual = response.json()["members"][0]
    assert actual["active"] == baseline.active_now
    for field in ("completed_total", "completed_recent", "on_time_rate", "sample_sufficient"):
        assert actual[field] == getattr(baseline, field)


async def test_overview_empty_and_constant_query_budget(client, admin_headers):
    response = await client.get("/api/v1/admin/overview", headers=admin_headers)
    assert response.json()["projects"] == response.json()["members"] == []
    async with async_session_factory() as session:
        user = User(username="many-projects", password_hash="unused")
        session.add(user)
        await session.flush()
        for index in range(20):
            project = Project(name=f"Project {index}")
            session.add(project)
            await session.flush()
            session.add(ProjectMember(project_id=project.id, user_id=user.id,
                                      display_name="Leader", role="leader"))
        await session.commit()
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with async_session_factory() as session:
            overview = await service.get_overview(session)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    assert len(overview.projects) == len(overview.members) == 20
    assert len(statements) <= 6


async def test_attention_pagination_and_summary(client, admin_headers, project_a, project_b, as_of):
    _, member = await add_member(project_a, "member", "Member123!")
    _, other = await add_member(project_b, "other", "Other123!")
    cases = [
        ("BLOCKED", as_of - timedelta(minutes=1), member, project_a),
        ("READY", as_of - timedelta(hours=2), member, project_a),
        ("BLOCKED", None, member, project_a),
        ("BLOCKED", as_of + timedelta(days=20), member, project_a),
        ("IN_REVIEW", as_of + timedelta(days=7), member, project_a),
        ("IN_PROGRESS", as_of, other, project_b),
        ("READY", as_of, other, project_b),
        ("DRAFT", as_of - timedelta(days=1), member, project_a),
        ("CANCELLED", as_of - timedelta(days=1), member, project_a),
        ("COMPLETED", as_of - timedelta(days=1), member, project_a),
        ("READY", as_of + timedelta(days=7, microseconds=1), member, project_a),
        ("IN_PROGRESS", None, member, project_a),
    ]
    ids = []
    async with async_session_factory() as session:
        for status, due_at, assignee, project in cases:
            item = WorkItem(project_id=project.id, assignee_id=assignee.id, title=status,
                            status=status, due_at=due_at, description="Large detail")
            session.add(item)
            await session.flush()
            ids.append(str(item.id))
        session.add(WorkItemCollaborator(work_item_id=uuid.UUID(ids[0]), member_id=member.id))
        await session.commit()
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        pages = [await client.get("/api/v1/admin/attention", headers=admin_headers,
                                  params={"limit": 2, "offset": offset})
                 for offset in range(0, 9, 2)]
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    assert all(page.status_code == 200 for page in pages)
    assert all(page.json()["total"] == 7 for page in pages)
    items = [item for page in pages for item in page.json()["items"]]
    assert [item["id"] for item in items] == [
        ids[0], ids[1], ids[3], ids[2], *sorted(ids[5:7]), ids[4]
    ]
    assert len({item["id"] for item in items}) == 7
    assert [item["is_overdue"] for item in items] == [True, True] + [False] * 5
    assert set(items[0]) == {"id", "project_id", "title", "status", "priority",
                            "assignee_id", "assignee_name", "due_at", "updated_at", "is_overdue"}
    assert items[0]["assignee_name"] == member.display_name
    assert all("work_items.description" not in statement for statement in statements)
    filtered = await client.get("/api/v1/admin/attention", headers=admin_headers,
                                params={"project_id": str(project_b.id)})
    assert filtered.json()["total"] == 2
    assert all(item["project_id"] == str(project_b.id) for item in filtered.json()["items"])
    missing = await client.get("/api/v1/admin/attention", headers=admin_headers,
                               params={"project_id": str(uuid.uuid4())})
    assert missing.json() == {"items": [], "total": 0}


@pytest.mark.parametrize("path", ["overview", "attention"])
async def test_admin_read_permissions(client, admin_headers, leader, project_a, path):
    url = f"/api/v1/admin/{path}"
    assert (await client.get(url)).status_code == 401
    headers = await auth_headers(client, "leader", "Leader123!", project_id=str(project_a.id))
    assert (await client.get(url, headers=headers)).status_code == 403
    await add_member(project_a, "regular", "Regular123!")
    headers = await auth_headers(client, "regular", "Regular123!", project_id=str(project_a.id))
    assert (await client.get(url, headers=headers)).status_code == 403
    assert (await client.get(url, headers=admin_headers)).status_code == 200


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": 201}, {"offset": -1}, {"project_id": "invalid"},
])
async def test_attention_validation(client, admin_headers, params):
    response = await client.get("/api/v1/admin/attention", headers=admin_headers, params=params)
    assert response.status_code == 422


async def test_audit_filters_stay_within_authorized_project(
    client, admin_headers, leader, project_a, project_b
):
    async with async_session_factory() as session:
        for project_id in (None, project_a.id, project_b.id):
            await record_event(session, action="test.filter", project_id=project_id)
        await session.commit()
    url = "/api/v1/audit-events"
    response = await client.get(url, headers=admin_headers)
    assert len(response.json()) == 3
    for project in (project_a, project_b):
        response = await client.get(url, headers=admin_headers,
                                    params={"project_id": str(project.id)})
        assert [row["project_id"] for row in response.json()] == [str(project.id)]
    response = await client.get(url, headers=admin_headers, params={"platform_only": True})
    assert [row["project_id"] for row in response.json()] == [None]

    headers = await auth_headers(client, "leader", "Leader123!", project_id=str(project_a.id))
    for params in ({}, {"project_id": str(project_a.id)}, {"platform_only": False}):
        response = await client.get(url, headers=headers, params=params)
        assert [row["project_id"] for row in response.json()] == [str(project_a.id)]
    for params in ({"project_id": str(project_b.id)}, {"platform_only": True}):
        response = await client.get(url, headers=headers, params=params)
        assert response.status_code == 200 and response.json() == []
    for auth in (headers, admin_headers):
        for params in ({"project_id": str(project_a.id), "platform_only": True},
                       {"project_id": "invalid"}):
            assert (await client.get(url, headers=auth, params=params)).status_code == 422
    no_project = {"Authorization": headers["Authorization"]}
    assert (await client.get(url, headers=no_project,
                             params={"project_id": str(project_a.id)})).status_code == 400
    forged = {**headers, "X-Project-Id": str(project_b.id)}
    assert (await client.get(url, headers=forged,
                             params={"project_id": str(project_a.id)})).status_code == 403
    await add_member(project_a, "member", "Member123!")
    member_headers = await auth_headers(client, "member", "Member123!", project_id=str(project_a.id))
    assert (await client.get(url, headers=member_headers,
                             params={"project_id": str(project_a.id)})).status_code == 403


async def test_audit_search_filters_before_pagination(client, admin_headers):
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        matches = [AuditEvent(action="needle.action", created_at=now - timedelta(days=i))
                   for i in (2, 3, 4)]
        session.add_all(matches)
        session.add_all([AuditEvent(action="unrelated", created_at=now) for _ in range(55)])
        await session.commit()
    url = "/api/v1/audit-events"
    first_page = await client.get(url, headers=admin_headers)
    assert len(first_page.json()) == 50
    assert all(row["action"] == "unrelated" for row in first_page.json())
    found = []
    for offset in range(4):
        response = await client.get(url, headers=admin_headers,
                                    params={"q": "  NeEdLe  ", "limit": 1, "offset": offset})
        assert response.status_code == 200
        found.extend(row["id"] for row in response.json())
    assert found == [str(row.id) for row in matches]


async def test_audit_search_fields_and_system_events(client, admin_headers, admin_user):
    target_id = uuid.uuid4()
    async with async_session_factory() as session:
        rows = [
            AuditEvent(action="account.changed", actor_id=admin_user.id),
            AuditEvent(action="System.UniqueAction"),
            AuditEvent(action="changed", target_type="UniqueTarget"),
            AuditEvent(action="changed", target_id=target_id),
            AuditEvent(action="changed", before={"name": "旧项目名称"}),
            AuditEvent(action="changed", after={"name": "NewProjectName"}),
        ]
        session.add_all(rows)
        await session.commit()
    for keyword, expected in zip(
        ("ADMIN", "uniqueaction", "uniquetarget", str(target_id).upper(), "旧项目名称", "newprojectname"),
        rows,
    ):
        response = await client.get("/api/v1/audit-events", headers=admin_headers,
                                    params={"q": keyword})
        assert response.status_code == 200
        assert [row["id"] for row in response.json()] == [str(expected.id)]


async def test_audit_search_preserves_project_and_platform_scope(
    client, admin_headers, leader, project_a, project_b
):
    async with async_session_factory() as session:
        for project_id in (None, project_a.id, project_b.id):
            await record_event(session, action="scope.match", project_id=project_id)
        await session.commit()
    url = "/api/v1/audit-events"
    for filters, expected in (
        ({}, {None, str(project_a.id), str(project_b.id)}),
        ({"project_id": str(project_a.id)}, {str(project_a.id)}),
        ({"project_id": str(project_b.id)}, {str(project_b.id)}),
        ({"platform_only": True}, {None}),
    ):
        response = await client.get(url, headers=admin_headers, params={"q": "match", **filters})
        assert response.status_code == 200
        assert {row["project_id"] for row in response.json()} == expected
    headers = await auth_headers(client, "leader", "Leader123!", project_id=str(project_a.id))
    for filters, expected in (
        ({}, [str(project_a.id)]),
        ({"project_id": str(project_a.id)}, [str(project_a.id)]),
        ({"project_id": str(project_b.id)}, []),
        ({"platform_only": True}, []),
    ):
        response = await client.get(url, headers=headers, params={"q": "match", **filters})
        assert response.status_code == 200
        assert [row["project_id"] for row in response.json()] == expected
    conflict = await client.get(url, headers=admin_headers, params={
        "q": "match", "project_id": str(project_a.id), "platform_only": True,
    })
    assert conflict.status_code == 422
    await add_member(project_a, "regular", "Regular123!")
    regular = await auth_headers(client, "regular", "Regular123!", project_id=str(project_a.id))
    assert (await client.get(url, headers=regular, params={"q": "match"})).status_code == 403


@pytest.mark.parametrize("keyword", ["%", "_", "/", "\\"])
async def test_audit_search_literal_wildcards(client, admin_headers, keyword):
    async with async_session_factory() as session:
        match = AuditEvent(action=f"prefix{keyword}suffix")
        session.add_all([match, AuditEvent(action="prefixXsuffix")])
        await session.commit()
    response = await client.get("/api/v1/audit-events", headers=admin_headers,
                                params={"q": keyword})
    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [str(match.id)]


async def test_audit_search_empty_and_length_validation(client, admin_headers):
    async with async_session_factory() as session:
        await record_event(session, action="visible", project_id=None)
        await session.commit()
    url = "/api/v1/audit-events"
    baseline = (await client.get(url, headers=admin_headers)).json()
    for keyword in ("", " \t "):
        response = await client.get(url, headers=admin_headers, params={"q": keyword})
        assert response.status_code == 200 and response.json() == baseline
    assert (await client.get(url, headers=admin_headers, params={"q": "x" * 200})).status_code == 200
    assert (await client.get(url, headers=admin_headers, params={"q": "x" * 201})).status_code == 422
