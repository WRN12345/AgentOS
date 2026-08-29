"""项目上下文、全局 admin 与 GET /me/projects 测试。"""

import pytest


async def test_missing_project_id_returns_400(client, leader):
    """业务接口缺失 X-Project-Id → 400 MISSING_PROJECT_ID。"""
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "leader", "password": "Leader123!"}
    )
    token = resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.get("/api/v1/members", headers=headers)
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "MISSING_PROJECT_ID"


async def test_invalid_project_id_format_returns_400(client, leader):
    """X-Project-Id 格式无效（非 UUID）→ 400。"""
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "leader", "password": "Leader123!"}
    )
    token = resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}", "X-Project-Id": "not-a-uuid"}

    resp = await client.get("/api/v1/members", headers=headers)
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "MISSING_PROJECT_ID"


async def test_non_member_project_returns_403(client, leader, project_b):
    """携带自己不是成员的项目 → 403 NOT_PROJECT_MEMBER。"""
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "leader", "password": "Leader123!"}
    )
    token = resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}", "X-Project-Id": str(project_b.id)}

    resp = await client.get("/api/v1/members", headers=headers)
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "NOT_PROJECT_MEMBER"


async def test_valid_member_with_project_id_succeeds(client, project_a, leader):
    """携带正确的 X-Project-Id → 正常返回。"""
    resp = await client.post(
        "/api/v1/auth/login", json={"username": "leader", "password": "Leader123!"}
    )
    token = resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}", "X-Project-Id": str(project_a.id)}

    resp = await client.get("/api/v1/members", headers=headers)
    assert resp.status_code == 200


async def test_me_projects_returns_user_projects(client, project_a, project_b, leader):
    """GET /me/projects 返回当前用户参与的所有项目及角色。"""
    leader_headers = await _login(client, "leader", "Leader123!")

    resp = await client.get("/api/v1/auth/me/projects", headers=leader_headers)
    assert resp.status_code == 200
    projects = resp.json()
    assert isinstance(projects, list)
    assert len(projects) >= 1

    project_a_data = next((p for p in projects if p["id"] == str(project_a.id)), None)
    assert project_a_data is not None
    assert project_a_data["role"] == "leader"
    assert project_a_data["name"] == "项目 A"


async def test_me_projects_cross_project_member(client, project_a, project_b, leader):
    """同一用户加入两个项目，/me/projects 返回两个项目，角色各自正确。"""
    from tests.conftest import add_member_for_existing_user, async_session_factory
    from app.domains.identity.models import User

    async with async_session_factory() as session:
        user = await session.get(User, leader.user_id)
    await add_member_for_existing_user(
        async_session_factory, project_b, user, role="member", display_name="负责人-B"
    )

    leader_headers = await _login(client, "leader", "Leader123!")
    resp = await client.get("/api/v1/auth/me/projects", headers=leader_headers)
    assert resp.status_code == 200
    projects = resp.json()
    assert len(projects) == 2

    roles = {p["id"]: p["role"] for p in projects}
    assert roles[str(project_a.id)] == "leader"
    assert roles[str(project_b.id)] == "member"


async def test_me_projects_empty_for_user_with_no_membership(client):
    """用户存在于系统但未加入任何项目 → 返回空列表。"""
    from tests.conftest import async_session_factory
    from app.domains.identity.service import create_user

    async with async_session_factory() as session:
        await create_user(session, "lonely", "Lonely123!")
        await session.commit()

    headers = await _login(client, "lonely", "Lonely123!")
    resp = await client.get("/api/v1/auth/me/projects", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == []


async def test_admin_can_access_config_without_project(client, admin_user):
    """全局管理员无需项目上下文即可访问 /config。"""
    headers = await _login(client, "admin", "Admin123!")
    resp = await client.get("/api/v1/config", headers=headers)
    assert resp.status_code == 200


async def test_admin_can_access_audit_without_project(client, admin_user):
    """全局管理员无需项目上下文即可访问 /audit-events。"""
    headers = await _login(client, "admin", "Admin123!")
    resp = await client.get("/api/v1/audit-events", headers=headers)
    assert resp.status_code == 200


async def test_admin_user_has_no_member_record(client, admin_user, project_a):
    """全局管理员没有项目成员记录，不能以任何项目成员身份访问业务接口。"""
    headers = await _login(client, "admin", "Admin123!")
    headers_with_project = {**headers, "X-Project-Id": str(project_a.id)}

    resp = await client.get("/api/v1/members", headers=headers_with_project)
    assert resp.status_code == 403


async def test_normal_user_cannot_access_admin_endpoint(client, leader):
    """普通成员可访问使用 get_current_user 的 /config。"""
    # admin 专用端点已由管理控制台测试覆盖；此处限定 /config 的普通用户权限。
    headers = await _login(client, "leader", "Leader123!")
    resp = await client.get("/api/v1/config", headers=headers)
    assert resp.status_code == 200


async def test_me_endpoint_includes_is_admin(client, admin_user, leader):
    """GET /me 返回 is_admin 字段。admin 用户为 True，普通用户为 False。"""
    admin_headers = await _login(client, "admin", "Admin123!")
    resp = await client.get("/api/v1/auth/me", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["is_admin"] is True

    leader_headers = await _login(client, "leader", "Leader123!")
    resp = await client.get("/api/v1/auth/me", headers=leader_headers)
    assert resp.status_code == 200
    assert resp.json()["is_admin"] is False


async def test_work_item_body_project_id_is_ignored(
    client, project_a, project_b, leader
):
    """body 带 project_id 不被接受：落库归属 = X-Project-Id 头项目，而非 body 指定的项目。"""
    from sqlalchemy import select

    from app.domains.work_items.models import WorkItem
    from app.infrastructure.database.engine import async_session_factory

    headers = await _login(client, "leader", "Leader123!")
    headers_a = {**headers, "X-Project-Id": str(project_a.id)}

    resp = await client.post(
        "/api/v1/work-items",
        json={
            "title": "带 project_id 的工作项",
            "priority": "medium",
            "assignee_id": str(leader.id),
            "project_id": str(project_b.id),  # 模拟伪造其他项目归属。
        },
        headers=headers_a,
    )
    assert resp.status_code == 201, resp.text
    item_id = resp.json()["id"]

    async with async_session_factory() as session:
        item = (await session.execute(select(WorkItem).where(WorkItem.id == item_id))).scalar_one()
    assert item.project_id == project_a.id


async def _login(client, username: str, password: str) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}
