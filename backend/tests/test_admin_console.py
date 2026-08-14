"""管理控制台（ticket 10）HTTP API 测试——主接缝。

覆盖规格 10-admin-console.md 四条验收：
- 管理控制台展示项目列表；
- admin 可创建项目并指定负责人；负责人/成员不能建；
- 账号管理与审计查看可用（admin-only 校验）；
- 新项目创建后，被指定的负责人可立即进入该项目工作台。
"""

import uuid
from typing import TypedDict

import httpx

from app.domains.identity.models import User
from app.domains.project.models import Project, ProjectMember
from tests.conftest import add_member, auth_headers, create_admin_user

LEADER_PW = "Leader123!"
ADMIN_PW = "Admin123!"
ALICE_PW = "Alice123!"
BOB_PW = "Bob123!"


class _AdminCtx(TypedDict):
    """标准场景成员与请求头（类型化，避免调用处反复 type: ignore）。"""

    leader: ProjectMember
    alice: ProjectMember
    bob: ProjectMember
    admin_user: User
    leader_headers: dict[str, str]
    admin_headers: dict[str, str]
    alice_headers: dict[str, str]


async def _make_ctx(client: httpx.AsyncClient, project: Project) -> _AdminCtx:
    """标准场景：leader + alice/bob 成员 + 全局 admin。"""
    _, leader = await add_member(project, "leader", LEADER_PW, role="leader", display_name="负责人")
    _, alice = await add_member(project, "alice", ALICE_PW, display_name="爱丽丝")
    _, bob = await add_member(project, "bob", BOB_PW, display_name="鲍勃")
    admin_user = await create_admin_user("admin", ADMIN_PW)

    return {
        "leader": leader,
        "alice": alice,
        "bob": bob,
        "admin_user": admin_user,
        "leader_headers": await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id)),
        "admin_headers": await auth_headers(client, "admin", ADMIN_PW),
        "alice_headers": await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id)),
    }


# ---------- 权限：仅 admin 可访问管理端点 ----------


async def test_normal_user_cannot_list_projects(client: httpx.AsyncClient, project: Project) -> None:
    """负责人/成员访问 GET /projects → 403。"""
    ctx = await _make_ctx(client, project)
    for headers in (ctx["leader_headers"], ctx["alice_headers"]):
        resp = await client.get("/api/v1/projects", headers=headers)
        assert resp.status_code == 403
        assert resp.json()["code"] == "FORBIDDEN"


async def test_normal_user_cannot_create_project(client: httpx.AsyncClient, project: Project) -> None:
    """负责人/成员 POST /projects → 403。"""
    ctx = await _make_ctx(client, project)
    payload = {"name": "越权项目", "owner_user_id": str(ctx["alice"].user_id)}
    for headers in (ctx["leader_headers"], ctx["alice_headers"]):
        resp = await client.post("/api/v1/projects", json=payload, headers=headers)
        assert resp.status_code == 403
        assert resp.json()["code"] == "FORBIDDEN"


async def test_unauthorized_list_projects(client: httpx.AsyncClient, project: Project) -> None:
    """未登录访问管理端点 → 401。"""
    resp = await client.get("/api/v1/projects")
    assert resp.status_code == 401


# ---------- 项目列表 ----------


async def test_admin_lists_projects(client: httpx.AsyncClient, project: Project) -> None:
    """admin 可见全部项目；无负责人的项目 leader 为 null，有负责人的带 leader 摘要。"""
    ctx = await _make_ctx(client, project)
    admin_headers = ctx["admin_headers"]

    resp = await client.get("/api/v1/projects", headers=admin_headers)
    assert resp.status_code == 200
    items = resp.json()
    assert isinstance(items, list)
    assert len(items) >= 1

    proj = next(p for p in items if p["id"] == str(project.id))
    # 已有 leader 成员（负责人）
    assert proj["leader"] is not None
    assert proj["leader"]["username"] == "leader"
    assert proj["leader"]["display_name"] == "负责人"
    assert proj["name"] == "项目 A"


async def test_project_list_includes_project_created_via_admin(
    client: httpx.AsyncClient, project: Project
) -> None:
    """通过管理接口创建的项目出现在项目列表中，且带指定负责人。"""
    ctx = await _make_ctx(client, project)
    admin_headers = ctx["admin_headers"]

    created = await client.post(
        "/api/v1/projects",
        json={"name": "新项目", "description": "由 admin 创建", "owner_user_id": str(ctx["alice"].user_id)},
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text

    resp = await client.get("/api/v1/projects", headers=admin_headers)
    items = resp.json()
    new_proj = next(p for p in items if p["id"] == created.json()["id"])
    assert new_proj["name"] == "新项目"
    assert new_proj["description"] == "由 admin 创建"
    assert new_proj["leader"]["username"] == "alice"


# ---------- 创建项目（指定负责人） ----------


async def test_admin_creates_project_with_owner(client: httpx.AsyncClient, project: Project) -> None:
    """admin 创建项目并指定负责人 → 201，负责人成为 leader 成员。"""
    ctx = await _make_ctx(client, project)
    admin_headers = ctx["admin_headers"]

    resp = await client.post(
        "/api/v1/projects",
        json={"name": "RAG 平台", "owner_user_id": str(ctx["alice"].user_id)},
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "RAG 平台"
    assert body["leader"]["user_id"] == str(ctx["alice"].user_id)
    # 负责人可在创建后立即进入该工作台（GET /me/projects 含该项目，角色为 leader）
    alice_headers = await auth_headers(client, "alice", ALICE_PW)
    me_projects = await client.get("/api/v1/auth/me/projects", headers=alice_headers)
    assert me_projects.status_code == 200
    assert any(p["id"] == body["id"] and p["role"] == "leader" for p in me_projects.json())

    # 携带新项目 X-Project-Id 可进入工作台（业务接口放行）
    alice_work_headers = {**alice_headers, "X-Project-Id": body["id"]}
    resp_members = await client.get("/api/v1/members", headers=alice_work_headers)
    assert resp_members.status_code == 200


async def test_create_project_with_unknown_owner_404(client: httpx.AsyncClient, project: Project) -> None:
    """指定不存在的负责人 → 404。"""
    ctx = await _make_ctx(client, project)
    admin_headers = ctx["admin_headers"]

    resp = await client.post(
        "/api/v1/projects",
        json={"name": "幽灵项目", "owner_user_id": str(uuid.uuid4())},
        headers=admin_headers,
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


async def test_create_project_with_admin_owner_400(client: httpx.AsyncClient, project: Project) -> None:
    """指定全局管理员为负责人 → 400（admin 不参与项目业务，不能作为项目负责人）。"""
    ctx = await _make_ctx(client, project)
    admin_headers = ctx["admin_headers"]

    resp = await client.post(
        "/api/v1/projects",
        json={"name": "指定 admin", "owner_user_id": str(ctx["admin_user"].id)},
        headers=admin_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "VALIDATION_ERROR"


async def test_create_project_with_disabled_owner_400(client: httpx.AsyncClient, project: Project) -> None:
    """指定已禁用账号为负责人 → 400。"""
    ctx = await _make_ctx(client, project)
    admin_headers = ctx["admin_headers"]

    # 先禁用 bob
    resp = await client.patch(
        f"/api/v1/users/{ctx['bob'].user_id}",
        json={"is_active": False},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    resp = await client.post(
        "/api/v1/projects",
        json={"name": "禁用负责人", "owner_user_id": str(ctx["bob"].user_id)},
        headers=admin_headers,
    )
    assert resp.status_code == 400


# ---------- 账号管理（admin-only） ----------


async def test_admin_lists_users(client: httpx.AsyncClient, project: Project) -> None:
    """admin 可见全部账号（含 admin 标记与启用状态），不含敏感字段。"""
    ctx = await _make_ctx(client, project)
    admin_headers = ctx["admin_headers"]

    resp = await client.get("/api/v1/users", headers=admin_headers)
    assert resp.status_code == 200
    users = resp.json()
    usernames = {u["username"] for u in users}
    assert usernames >= {"admin", "leader", "alice", "bob"}
    admin_row = next(u for u in users if u["username"] == "admin")
    assert admin_row["is_admin"] is True
    assert "password_hash" not in admin_row
    assert "password" not in admin_row


async def test_normal_user_cannot_list_users(client: httpx.AsyncClient, project: Project) -> None:
    """负责人/成员访问 GET /users → 403。"""
    ctx = await _make_ctx(client, project)
    for headers in (ctx["leader_headers"], ctx["alice_headers"]):
        resp = await client.get("/api/v1/users", headers=headers)
        assert resp.status_code == 403
        assert resp.json()["code"] == "FORBIDDEN"


async def test_admin_disables_user_login_blocked(client: httpx.AsyncClient, project: Project) -> None:
    """admin 禁用账号后，该用户无法再登录。"""
    ctx = await _make_ctx(client, project)
    admin_headers = ctx["admin_headers"]

    resp = await client.patch(
        f"/api/v1/users/{ctx['alice'].user_id}",
        json={"is_active": False},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False

    login = await client.post(
        "/api/v1/auth/login", json={"username": "alice", "password": ALICE_PW}
    )
    assert login.status_code == 403
    assert login.json()["code"] == "USER_DISABLED"


async def test_admin_re_enables_user(client: httpx.AsyncClient, project: Project) -> None:
    """admin 重新启用账号后，用户可恢复登录。"""
    ctx = await _make_ctx(client, project)
    admin_headers = ctx["admin_headers"]

    await client.patch(
        f"/api/v1/users/{ctx['alice'].user_id}",
        json={"is_active": False},
        headers=admin_headers,
    )
    resp = await client.patch(
        f"/api/v1/users/{ctx['alice'].user_id}",
        json={"is_active": True},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True

    login = await client.post(
        "/api/v1/auth/login", json={"username": "alice", "password": ALICE_PW}
    )
    assert login.status_code == 200


async def test_admin_cannot_disable_self(client: httpx.AsyncClient, project: Project) -> None:
    """admin 不能禁用当前登录的管理员自己 → 400（避免锁死自己）。"""
    ctx = await _make_ctx(client, project)
    admin_headers = ctx["admin_headers"]

    resp = await client.patch(
        f"/api/v1/users/{ctx['admin_user'].id}",
        json={"is_active": False},
        headers=admin_headers,
    )
    assert resp.status_code == 400


async def test_disable_unknown_user_404(client: httpx.AsyncClient, project: Project) -> None:
    """禁用不存在的账号 → 404。"""
    ctx = await _make_ctx(client, project)
    admin_headers = ctx["admin_headers"]

    resp = await client.patch(
        f"/api/v1/users/{uuid.uuid4()}", json={"is_active": False}, headers=admin_headers
    )
    assert resp.status_code == 404


# ---------- 审计查看（admin 全量可见） ----------


async def test_admin_sees_audit_for_created_project(client: httpx.AsyncClient, project: Project) -> None:
    """创建项目产生审计事件，admin 在 /audit-events 可见。"""
    ctx = await _make_ctx(client, project)
    admin_headers = ctx["admin_headers"]

    await client.post(
        "/api/v1/projects",
        json={"name": "审计项目", "owner_user_id": str(ctx["alice"].user_id)},
        headers=admin_headers,
    )

    resp = await client.get("/api/v1/audit-events", headers=admin_headers)
    assert resp.status_code == 200
    events = resp.json()
    assert any(e["action"] == "project.created" for e in events)


async def test_normal_user_cannot_see_global_audit(client: httpx.AsyncClient, project: Project) -> None:
    """负责人访问 /audit-events 只见本项目事件；全局（project_id=NULL）事件不可见。"""
    ctx = await _make_ctx(client, project)
    admin_headers = ctx["admin_headers"]

    # admin 创建项目（全局事件，project_id=NULL）
    await client.post(
        "/api/v1/projects",
        json={"name": "全局审计", "owner_user_id": str(ctx["alice"].user_id)},
        headers=admin_headers,
    )

    leader_headers = ctx["leader_headers"]
    resp = await client.get("/api/v1/audit-events", headers=leader_headers)
    assert resp.status_code == 200
    # 全局事件对负责人不可见（隔离：墙外事件等同不存在）
    assert not any(e["action"] == "project.created" for e in resp.json())
