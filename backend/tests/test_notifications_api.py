from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.domains.identity.models import User
from app.domains.notifications.models import Notification
from app.domains.project.models import Project, ProjectMember
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member, add_member_for_existing_user, auth_headers


@pytest.mark.parametrize("role,is_admin", [("member", False), ("leader", False), ("member", True)])
async def test_read_all_marks_every_unread_and_preserves_read_times(
    client: httpx.AsyncClient, project: Project, role: str, is_admin: bool
) -> None:
    user, member = await add_member(project, "reader", "Reader123!", role=role)
    original_read_at = datetime(2026, 1, 1, tzinfo=UTC)
    async with async_session_factory() as session:
        if is_admin:
            stored_user = await session.get(User, user.id)
            assert stored_user is not None
            stored_user.is_admin = True
        notices = [
            Notification(
                project_id=project.id, recipient_id=member.id,
                type="test", title=f"Notice {i}", body="Test notification",
            )
            for i in range(25)
        ]
        already_read = Notification(
            project_id=project.id, recipient_id=member.id,
            type="test", title="Already read", body="Test notification",
            is_read=True, read_at=original_read_at,
        )
        session.add_all([*notices, already_read])
        await session.commit()
        already_read_id = already_read.id

    headers = await auth_headers(client, "reader", "Reader123!", project_id=str(project.id))
    before = await client.get("/api/v1/notifications?limit=20", headers=headers)
    assert before.status_code == 200
    assert len(before.json()["items"]) == 20
    assert before.json()["unread_count"] == 25

    started_at = datetime.now(UTC)
    response = await client.post("/api/v1/notifications/read-all", headers=headers)
    finished_at = datetime.now(UTC)
    assert response.status_code == 204, response.text
    assert response.content == b""
    async with async_session_factory() as session:
        rows = (await session.scalars(select(Notification))).all()
        assert len(rows) == 26
        assert all(row.is_read for row in rows)
        read_times = {row.id: row.read_at for row in rows}
        assert read_times[already_read_id] == original_read_at
        for row in rows:
            assert row.read_at is not None
            assert row.read_at.utcoffset() == timedelta(0)
            if row.id != already_read_id:
                assert started_at <= row.read_at <= finished_at

    after = await client.get("/api/v1/notifications?unread_only=true", headers=headers)
    assert after.status_code == 200
    assert after.json() == {"items": [], "unread_count": 0}
    repeated = await client.post("/api/v1/notifications/read-all", headers=headers)
    assert repeated.status_code == 204
    assert repeated.content == b""
    async with async_session_factory() as session:
        rows = (await session.scalars(select(Notification))).all()
        assert {row.id: row.read_at for row in rows} == read_times


async def test_read_all_isolates_recipient_and_project(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    user, member_a = await add_member(project_a, "reader", "Reader123!")
    _, other_member = await add_member(project_a, "other", "Other123!")
    member_b = await add_member_for_existing_user(async_session_factory, project_b, user)
    async with async_session_factory() as session:
        notices = [
            Notification(
                project_id=project_id, recipient_id=recipient_id,
                type="test", title="Notice", body="Test notification",
            )
            for project_id, recipient_id in [
                (project_a.id, member_a.id),
                (project_a.id, other_member.id),
                (project_b.id, member_b.id),
                # Verify the project predicate independently of recipient identity.
                (project_b.id, member_a.id),
            ]
        ]
        session.add_all(notices)
        await session.commit()
        target_id = notices[0].id

    headers = await auth_headers(client, "reader", "Reader123!", project_id=str(project_a.id))
    response = await client.post("/api/v1/notifications/read-all", headers=headers)
    assert response.status_code == 204, response.text
    async with async_session_factory() as session:
        rows = (await session.scalars(select(Notification))).all()
        assert len(rows) == 4
        for row in rows:
            assert row.is_read == (row.id == target_id)
            assert (row.read_at is not None) == (row.id == target_id)


async def test_read_all_empty_is_idempotent(client: httpx.AsyncClient, project: Project) -> None:
    await add_member(project, "reader", "Reader123!")
    headers = await auth_headers(client, "reader", "Reader123!", project_id=str(project.id))
    for _ in range(2):
        response = await client.post("/api/v1/notifications/read-all", headers=headers)
        assert response.status_code == 204, response.text
        assert response.content == b""


async def test_read_all_requires_active_project_membership(
    client: httpx.AsyncClient, project: Project, admin_headers: dict[str, str]
) -> None:
    unauthenticated = await client.post("/api/v1/notifications/read-all")
    assert unauthenticated.status_code == 401
    nonmember = await client.post(
        "/api/v1/notifications/read-all",
        headers={**admin_headers, "X-Project-Id": str(project.id)},
    )
    assert nonmember.status_code == 403

    _, member = await add_member(project, "reader", "Reader123!")
    headers = await auth_headers(client, "reader", "Reader123!", project_id=str(project.id))
    async with async_session_factory() as session:
        stored_member = await session.get(ProjectMember, member.id)
        assert stored_member is not None
        stored_member.is_active = False
        await session.commit()
    inactive = await client.post("/api/v1/notifications/read-all", headers=headers)
    assert inactive.status_code == 403
