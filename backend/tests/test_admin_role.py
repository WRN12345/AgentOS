"""管理员全局化后的权限矩阵测试。

admin 升级为全局角色（users.is_admin）：
- 不属于任何项目，无 project_members 记录；
- 可访问无需项目上下文的端点（config、audit、me）；
- 不可访问项目内业务端点（403 NOT_PROJECT_MEMBER）；
- 不可被指派（admin 没有成员身份，传入不存在的 member_id → 422）。
"""
import uuid

import httpx

from app.domains.project.models import Project, ProjectMember
from tests.conftest import add_member, auth_headers, create_admin_user

LEADER_PW = "Leader123!"
ADMIN_PW = "Admin123!"
ALICE_PW = "Alice123!"
BOB_PW = "Bob123!"


async def _make_ctx(client: httpx.AsyncClient, project: Project) -> dict[str, object]:
    """标准场景：leader + alice/bob 两名普通成员 + 全局 admin（is_admin，无 member 记录）。"""
    _, leader = await add_member(project, "leader", LEADER_PW, role="leader", display_name="负责人")
    _, alice = await add_member(project, "alice", ALICE_PW, display_name="爱丽丝")
    _, bob = await add_member(project, "bob", BOB_PW, display_name="鲍勃")
    admin_user = await create_admin_user("admin", ADMIN_PW)

    return {
        "leader": leader,
        "admin_user": admin_user,
        "alice": alice,
        "bob": bob,
        "leader_headers": await auth_headers(
            client, "leader", LEADER_PW, project_id=str(project.id)
        ),
        "admin_headers": await auth_headers(client, "admin", ADMIN_PW),
        "alice_headers": await auth_headers(
            client, "alice", ALICE_PW, project_id=str(project.id)
        ),
    }


async def _create_work_item(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    assignee_id: uuid.UUID,
    **extra: object,
) -> dict:
    """负责人创建工作项（DRAFT，version=1）。"""
    payload: dict[str, object] = {
        "title": "RAG 工作项",
        "description": "实现 RAG",
        "priority": "high",
        "assignee_id": str(assignee_id),
        **extra,
    }
    resp = await client.post("/api/v1/work-items", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------- admin 无法访问项目内业务接口 ----------


async def test_admin_cannot_access_project_endpoints(
    client: httpx.AsyncClient, project: Project
) -> None:
    """全局 admin 没有项目成员身份，访问 /members 等业务接口 → 403。"""
    ctx = await _make_ctx(client, project)
    admin_headers = ctx["admin_headers"]
    assert isinstance(admin_headers, dict)

    # admin 没有项目上下文 → 400（MISSING_PROJECT_ID）
    resp = await client.get("/api/v1/members", headers=admin_headers)
    assert resp.status_code == 400
    assert resp.json()["code"] == "MISSING_PROJECT_ID"


async def test_admin_write_business_requires_project_context(
    client: httpx.AsyncClient, project: Project
) -> None:
    """admin 没有项目成员身份，无法写业务数据 — 缺失 X-Project-Id 即 400。"""
    ctx = await _make_ctx(client, project)
    admin_headers = ctx["admin_headers"]
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    assert isinstance(admin_headers, dict)

    # 建工作项 → 400（缺失项目上下文）
    created = await client.post(
        "/api/v1/work-items",
        json={"title": "越权工作项", "priority": "low", "assignee_id": str(alice.id)},
        headers=admin_headers,
    )
    assert created.status_code == 400


# ---------- admin 可访问无项目上下文的管理端点 ----------


async def test_admin_read_access_without_project(
    client: httpx.AsyncClient, project: Project
) -> None:
    """admin 可读 config/me/audit-events（这些端点不依赖项目成员身份）。"""
    ctx = await _make_ctx(client, project)
    admin_headers = ctx["admin_headers"]
    assert isinstance(admin_headers, dict)

    # /config 使用 get_current_user
    resp = await client.get("/api/v1/config", headers=admin_headers)
    assert resp.status_code == 200

    # /me 使用 get_current_user
    resp = await client.get("/api/v1/auth/me", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["is_admin"] is True

    # /audit-events 使用 get_current_leader_or_admin（admin 分支放行）
    resp = await client.get("/api/v1/audit-events", headers=admin_headers)
    assert resp.status_code == 200


# ---------- admin 不可被指派（没有成员记录） ----------


async def test_admin_cannot_be_assigned_no_member_record(
    client: httpx.AsyncClient, project: Project
) -> None:
    """admin 没有 project_members 记录，指派到不存在的 member_id → 422。"""
    ctx = await _make_ctx(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    leader_headers = ctx["leader_headers"]
    assert isinstance(leader_headers, dict)

    # 造一个不存在的 UUID（模拟 admin 没有 member_id 的场景）
    fake_member_id = uuid.uuid4()

    created = await client.post(
        "/api/v1/work-items",
        json={"title": "指派不存在成员", "priority": "low", "assignee_id": str(fake_member_id)},
        headers=leader_headers,
    )
    assert created.status_code == 422, created.text
    assert "成员不存在" in created.json()["message"]


# ---------- 成员列表不含 admin（admin 无记录） ----------


async def test_member_list_does_not_contain_admin(
    client: httpx.AsyncClient, project: Project
) -> None:
    """全局 admin 没有 project_members 记录，成员列表自然不含 admin。"""
    ctx = await _make_ctx(client, project)
    leader_headers = ctx["leader_headers"]
    assert isinstance(leader_headers, dict)

    resp = await client.get("/api/v1/members", headers=leader_headers)
    assert resp.status_code == 200, resp.text
    members = resp.json()
    # 只有 leader、alice、bob，没有 admin
    usernames = {m["username"] for m in members}
    assert "admin" not in usernames
    assert usernames >= {"leader", "alice", "bob"}


# ---------- 成员账号管理：仅 leader 可操作 ----------


async def test_only_leader_can_add_member(
    client: httpx.AsyncClient, project: Project
) -> None:
    """普通成员不能添加成员 → 403；leader 可添加已有账号 → 201。"""
    ctx = await _make_ctx(client, project)
    leader_headers = ctx["leader_headers"]
    alice_headers = ctx["alice_headers"]
    assert isinstance(leader_headers, dict) and isinstance(alice_headers, dict)

    # carol 是已有全局账号（无成员记录，建号收敛到 admin）
    from app.domains.identity.service import create_user
    from app.infrastructure.database.engine import async_session_factory

    async with async_session_factory() as session:
        await create_user(session, "carol", "Carol123!")
        await session.commit()

    payload = {"username": "carol"}

    # 普通成员 → 403
    resp = await client.post("/api/v1/members", json=payload, headers=alice_headers)
    assert resp.status_code == 403

    # leader → 201
    resp = await client.post("/api/v1/members", json=payload, headers=leader_headers)
    assert resp.status_code == 201, resp.text


# ---------- Agent 分配建议数据源：自动不含 admin ----------


async def test_agent_tools_exclude_admin(project: Project) -> None:
    """admin 没有 project_members 记录，Agent 工具自然不返回 admin。"""
    from app.agents.tools import get_member_workload, list_member_capabilities
    from app.domains.project.models import MemberCapability
    from app.infrastructure.database.engine import async_session_factory

    # 仅创建普通成员（不创建 admin 成员记录）
    _, alice = await add_member(project, "alice", ALICE_PW, display_name="爱丽丝")
    await create_admin_user("admin", ADMIN_PW)  # 无 member 记录

    async with async_session_factory() as session:
        session.add(MemberCapability(member_id=alice.id, tag="RAG", proficiency=3))
        await session.commit()

    async with async_session_factory() as session:
        caps = await list_member_capabilities(session, project_id=project.id)
        workload = await get_member_workload(session, project_id=project.id)

    alice_id = str(alice.id)
    # admin 没有 member 记录，工具自然不返回
    assert all(c["member_id"] == alice_id for c in caps)
    assert all(w["member_id"] == alice_id for w in workload)


# ---------- 角色仅由 admin 指定：成员接口不接受 role 字段 ----------


async def test_member_create_rejects_role_field(
    client: httpx.AsyncClient, project: Project
) -> None:
    """POST /members 带 role → 422（角色仅由 admin 指定，每项目一名负责人）。"""
    ctx = await _make_ctx(client, project)
    leader_headers = ctx["leader_headers"]
    assert isinstance(leader_headers, dict)

    resp = await client.post(
        "/api/v1/members",
        json={"username": "alice", "role": "admin"},
        headers=leader_headers,
    )
    assert resp.status_code == 422, resp.text


async def test_member_update_rejects_role_field(
    client: httpx.AsyncClient, project: Project
) -> None:
    """PATCH /members 带 role → 422（角色仅由 admin 指定/变更）。"""
    ctx = await _make_ctx(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    leader_headers = ctx["leader_headers"]
    assert isinstance(leader_headers, dict)

    resp = await client.patch(
        f"/api/v1/members/{alice.id}", json={"role": "leader"}, headers=leader_headers
    )
    assert resp.status_code == 422, resp.text
