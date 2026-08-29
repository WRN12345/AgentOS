"""成员列表、写路径、账号复用、角色和输入字段的项目隔离测试。"""

import uuid

import httpx
from sqlalchemy import select

from app.domains.project.models import Project
from app.domains.work_items.models import WorkItem
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member, auth_headers, create_admin_user

LEADER_PW = "Leader123!"
ALICE_PW = "Alice123!"
MEMBER_PW = "Member123!"


async def _setup_project(
    client: httpx.AsyncClient, project: Project, *, tag: str
) -> dict[str, object]:
    """返回指定项目的负责人和两名成员，使用 tag 隔离用户名。"""
    _, leader = await add_member(
        project, f"{tag}_leader", LEADER_PW, role="leader", display_name="负责人"
    )
    _, alice = await add_member(project, f"{tag}_alice", ALICE_PW, display_name="爱丽丝")
    _, bob = await add_member(project, f"{tag}_bob", "Bob123!", display_name="鲍勃")
    return {
        "leader": leader,
        "alice": alice,
        "bob": bob,
        "leader_headers": await auth_headers(
            client, f"{tag}_leader", LEADER_PW, project_id=str(project.id)
        ),
        "alice_headers": await auth_headers(
            client, f"{tag}_alice", ALICE_PW, project_id=str(project.id)
        ),
    }


async def test_members_list_isolated_between_projects(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """A 上下文只列出 A 的成员，B 上下文只列出 B 的成员（不跨项目泄漏）。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")

    resp_a = await client.get("/api/v1/members", headers=ctx_a["leader_headers"])
    assert resp_a.status_code == 200
    users_a = {m["username"] for m in resp_a.json()}
    assert users_a == {"a_leader", "a_alice", "a_bob"}

    resp_b = await client.get("/api/v1/members", headers=ctx_b["leader_headers"])
    assert resp_b.status_code == 200
    users_b = {m["username"] for m in resp_b.json()}
    assert users_b == {"b_leader", "b_alice", "b_bob"}


async def test_members_list_active_load_only_this_project(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """active_work_items 负载统计只统计本项目工作项（不跨项目计入）。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")

    for project, ctx in ((project_a, ctx_a), (project_b, ctx_b)):
        async with async_session_factory() as session:
            session.add(
                WorkItem(
                    title="负载测试",
                    description="",
                    project_id=project.id,
                    assignee_id=ctx["alice"].id,  # type: ignore[union-attr]
                    status="IN_PROGRESS",
                )
            )
            await session.commit()

    resp_a = await client.get("/api/v1/members", headers=ctx_a["leader_headers"])
    assert resp_a.status_code == 200
    alice_a = next(m for m in resp_a.json() if m["username"] == "a_alice")
    assert alice_a["active_work_items"] == 1  # 只算 A 自己的工作项
    bob_a = next(m for m in resp_a.json() if m["username"] == "a_bob")
    assert bob_a["active_work_items"] == 0


async def test_cross_project_patch_member_returns_404(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """A 负责人 PATCH B 项目成员 → 404（越权即不存在，不泄露存在性）。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")

    resp = await client.patch(
        f"/api/v1/members/{ctx_b['alice'].id}",  # type: ignore[union-attr]
        json={"display_name": "越权改名"},
        headers=ctx_a["leader_headers"],
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"

    ok = await client.patch(
        f"/api/v1/members/{ctx_b['alice'].id}",  # type: ignore[union-attr]
        json={"display_name": "正常改名"},
        headers=ctx_b["leader_headers"],
    )
    assert ok.status_code == 200


async def test_cross_project_put_capabilities_returns_404(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """A 负责人 PUT B 项目成员能力集 → 404。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")

    resp = await client.put(
        f"/api/v1/members/{ctx_b['alice'].id}/capabilities",  # type: ignore[union-attr]
        json={"capabilities": [{"tag": "RAG", "proficiency": 3}]},
        headers=ctx_a["leader_headers"],
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


async def test_leader_cannot_disable_current_project_leader(
    client: httpx.AsyncClient, project_a: Project
) -> None:
    """现任负责人不可被项目成员接口禁用，避免项目进入无人可管理状态。"""
    ctx = await _setup_project(client, project_a, tag="guard")
    response = await client.patch(
        f"/api/v1/members/{ctx['leader'].id}",
        json={"is_active": False},
        headers=ctx["leader_headers"],
    )
    assert response.status_code == 409
    assert response.json()["code"] == "PROJECT_LEADER_REQUIRED"


async def test_disable_member_project_local_across_projects(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """A 负责人禁用 a_alice 仅停 A 本项目：账号仍可登录、B 上下文照常、/me/projects 不再含 A。

    全局禁用账号会阻止登录，项目内禁用仅影响对应项目，不影响账号与其他项目。
    """
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")

    joined = await client.post(
        "/api/v1/members",
        json={"username": "a_alice"},
        headers=ctx_b["leader_headers"],
    )
    assert joined.status_code == 201, joined.text

    disabled = await client.patch(
        f"/api/v1/members/{ctx_a['alice'].id}",  # type: ignore[union-attr]
        json={"is_active": False},
        headers=ctx_a["leader_headers"],
    )
    assert disabled.status_code == 200
    assert disabled.json()["is_active"] is False

    login = await client.post(
        "/api/v1/auth/login", json={"username": "a_alice", "password": ALICE_PW}
    )
    assert login.status_code == 200

    blocked = await client.get("/api/v1/members", headers=ctx_a["alice_headers"])
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "NOT_PROJECT_MEMBER"

    alice_b_headers = await auth_headers(
        client, "a_alice", ALICE_PW, project_id=str(project_b.id)
    )
    ok = await client.get("/api/v1/members", headers=alice_b_headers)
    assert ok.status_code == 200

    alice_headers = await auth_headers(client, "a_alice", ALICE_PW)
    me_projects = await client.get("/api/v1/auth/me/projects", headers=alice_headers)
    assert me_projects.status_code == 200
    assert {p["id"] for p in me_projects.json()} == {str(project_b.id)}


async def test_reuse_by_username_adds_to_second_project(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """A 已有成员（全局账号）由 B 负责人按用户名加入 B，无初始密码，/me/projects 双项目。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")

    joined = await client.post(
        "/api/v1/members",
        json={"username": "a_alice", "display_name": "爱丽丝-B", "git_username": "alice-git-b"},
        headers=ctx_b["leader_headers"],
    )
    assert joined.status_code == 201, joined.text
    body = joined.json()
    assert body["username"] == "a_alice"
    assert body["role"] == "member"
    assert "initial_password" not in body  # 复用已有账号无初始密码
    assert body["display_name"] == "爱丽丝-B"

    alice_headers = await auth_headers(client, "a_alice", ALICE_PW)
    resp = await client.get("/api/v1/auth/me/projects", headers=alice_headers)
    assert resp.status_code == 200
    assert {p["id"] for p in resp.json()} == {str(project_a.id), str(project_b.id)}


async def test_duplicate_username_blocked_at_admin_create(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """admin 建号全局唯一：重名 → 409；负责人可按用户名复用同一账号加入 B。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")
    await create_admin_user("scope_admin", "Admin123!")
    admin_headers = await auth_headers(client, "scope_admin", "Admin123!")

    created = await client.post(
        "/api/v1/users",
        json={"username": "dup_user", "password": "DupUser123!"},
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text

    dup = await client.post(
        "/api/v1/users",
        json={"username": "dup_user", "password": "DupUser123!"},
        headers=admin_headers,
    )
    assert dup.status_code == 409
    assert dup.json()["code"] == "USERNAME_TAKEN"

    joined = await client.post(
        "/api/v1/members",
        json={"username": "dup_user"},
        headers=ctx_b["leader_headers"],
    )
    assert joined.status_code == 201, joined.text
    assert joined.json()["username"] == "dup_user"


async def test_member_create_identity_validation(
    client: httpx.AsyncClient, project_a: Project
) -> None:
    """username/user_id 全给或全缺 → 422；带 password/role 等多余字段 → 422；不存在 → 404。"""
    ctx_a = await _setup_project(client, project_a, tag="a")

    resp = await client.post(
        "/api/v1/members",
        json={"username": "no_pw", "password": "Pw123456!", "display_name": "建号"},
        headers=ctx_a["leader_headers"],
    )
    assert resp.status_code == 422, resp.text

    resp = await client.post(
        "/api/v1/members",
        json={"username": "a_alice", "role": "leader"},
        headers=ctx_a["leader_headers"],
    )
    assert resp.status_code == 422, resp.text

    resp = await client.post(
        "/api/v1/members",
        json={
            "username": "a_alice",
            "user_id": str(ctx_a["alice"].user_id),  # type: ignore[union-attr]
        },
        headers=ctx_a["leader_headers"],
    )
    assert resp.status_code == 422, resp.text

    resp = await client.post(
        "/api/v1/members",
        json={"display_name": "缺身份"},
        headers=ctx_a["leader_headers"],
    )
    assert resp.status_code == 422, resp.text

    resp = await client.post(
        "/api/v1/members",
        json={"username": "ghost_user"},
        headers=ctx_a["leader_headers"],
    )
    assert resp.status_code == 404, resp.text


async def test_reuse_existing_user_id_joins_second_project(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """admin 建全局账号 carol；A、B 负责人分别按 username/user_id 加入，/me/projects 双项目。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")
    await create_admin_user("scope_admin", "Admin123!")
    admin_headers = await auth_headers(client, "scope_admin", "Admin123!")

    created = await client.post(
        "/api/v1/users",
        json={"username": "carol", "password": MEMBER_PW},
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    carol_user_id = created.json()["id"]

    joined_a = await client.post(
        "/api/v1/members",
        json={"username": "carol", "git_username": "carol-git"},
        headers=ctx_a["leader_headers"],
    )
    assert joined_a.status_code == 201, joined_a.text
    joined = await client.post(
        "/api/v1/members",
        json={"user_id": carol_user_id, "git_username": "carol-git-b"},
        headers=ctx_b["leader_headers"],
    )
    assert joined.status_code == 201, joined.text
    body = joined.json()
    assert body["user_id"] == carol_user_id
    assert body["role"] == "member"  # 固定成员角色
    assert "initial_password" not in body  # 复用账号无初始密码
    assert body["display_name"] == "carol"  # 未传 display_name 则回退全局账号 username

    carol_headers = await auth_headers(client, "carol", MEMBER_PW)
    resp = await client.get("/api/v1/auth/me/projects", headers=carol_headers)
    assert resp.status_code == 200
    projects = resp.json()
    assert {p["id"] for p in projects} == {str(project_a.id), str(project_b.id)}


async def test_reuse_user_id_conflict_admin_and_missing(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """user_id 已是本项目成员 → 409；是全局 admin → 400；不存在 → 404。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")

    resp = await client.post(
        "/api/v1/members",
        json={"user_id": str(ctx_b["alice"].user_id)},  # type: ignore[union-attr]
        headers=ctx_b["leader_headers"],
    )
    assert resp.status_code == 409

    admin_user = await create_admin_user("scope_admin", "Admin123!")
    resp = await client.post(
        "/api/v1/members",
        json={"user_id": str(admin_user.id)},
        headers=ctx_a["leader_headers"],
    )
    assert resp.status_code == 400

    resp = await client.post(
        "/api/v1/members",
        json={"user_id": str(uuid.uuid4())},
        headers=ctx_a["leader_headers"],
    )
    assert resp.status_code == 404


async def test_leader_cannot_set_role(client: httpx.AsyncClient, project_a: Project) -> None:
    """POST/PATCH /members 不接受 role 字段（角色仅由 admin 指定）→ 422，全项目仍一名负责人。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    alice_id = str(ctx_a["alice"].id)  # type: ignore[union-attr]

    create_resp = await client.post(
        "/api/v1/members",
        json={"username": "a_alice", "role": "leader"},
        headers=ctx_a["leader_headers"],
    )
    assert create_resp.status_code == 422, create_resp.text

    patch_resp = await client.patch(
        f"/api/v1/members/{alice_id}", json={"role": "leader"}, headers=ctx_a["leader_headers"]
    )
    assert patch_resp.status_code == 422, patch_resp.text

    members = await client.get("/api/v1/members", headers=ctx_a["leader_headers"])
    roles = [m["role"] for m in members.json()]
    assert roles.count("leader") == 1


async def test_member_rejects_project_id_in_body(
    client: httpx.AsyncClient, project_a: Project
) -> None:
    """POST/PATCH 不接受传入 project_id（extra=forbid，归属由请求上下文决定）。"""
    ctx_a = await _setup_project(client, project_a, tag="a")

    resp = await client.post(
        "/api/v1/members",
        json={"username": "a_alice", "project_id": str(project_a.id)},
        headers=ctx_a["leader_headers"],
    )
    assert resp.status_code == 422, resp.text

    resp = await client.patch(
        f"/api/v1/members/{ctx_a['alice'].id}",  # type: ignore[union-attr]
        json={"project_id": str(project_a.id)},
        headers=ctx_a["leader_headers"],
    )
    assert resp.status_code == 422, resp.text
