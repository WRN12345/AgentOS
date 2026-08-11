"""成员与能力管理集成测试（T2.3 验收，6.1、6.2、12.2、16 节）。"""

import httpx
from sqlalchemy import select

from app.domains.audit.models import AuditEvent
from app.domains.project.models import Project, ProjectMember
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member, auth_headers

LEADER_PW = "Leader123!"
MEMBER_PW = "Member123!"


async def _create_member_via_api(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    username: str = "alice",
    password: str = MEMBER_PW,
    **extra: object,
) -> httpx.Response:
    payload = {
        "username": username,
        "password": password,
        "display_name": username,
        "weekly_available_hours": 30,
        "git_username": f"{username}-git",
        **extra,
    }
    return await client.post("/api/v1/members", json=payload, headers=headers)


async def test_member_cannot_create_member(
    client: httpx.AsyncClient, project: Project, leader: ProjectMember
) -> None:
    """普通成员调用 POST /members → 403。"""
    leader_headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id))
    created = await _create_member_via_api(client, leader_headers)
    assert created.status_code == 201

    member_headers = await auth_headers(client, "alice", MEMBER_PW, project_id=str(project.id))
    resp = await _create_member_via_api(client, member_headers, username="bob")
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"


async def test_leader_creates_member_with_login_account(
    client: httpx.AsyncClient, project: Project, leader: ProjectMember
) -> None:
    """负责人创建成员并生成登录账号；初始密码仅创建响应返回一次，可登录。"""
    leader_headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id))
    resp = await _create_member_via_api(client, leader_headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["initial_password"] == MEMBER_PW
    assert body["role"] == "member"
    assert body["capabilities"] == []
    assert "password_hash" not in body
    assert "password" not in {k for k in body if k != "initial_password"}

    login = await client.post(
        "/api/v1/auth/login", json={"username": "alice", "password": MEMBER_PW}
    )
    assert login.status_code == 200

    # 审计事件留痕
    async with async_session_factory() as session:
        events = (
            (await session.execute(select(AuditEvent).where(AuditEvent.action == "member.created")))
            .scalars()
            .all()
        )
    assert len(events) == 1
    assert events[0].actor_id == leader.user_id
    assert events[0].target_type == "project_member"
    assert events[0].after["username"] == "alice"


async def test_create_member_username_conflict(
    client: httpx.AsyncClient, project: Project, leader: ProjectMember
) -> None:
    leader_headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id))
    resp = await _create_member_via_api(client, leader_headers, username="leader")
    assert resp.status_code == 409
    assert resp.json()["code"] == "USERNAME_TAKEN"


async def test_get_members_returns_summary_without_sensitive_fields(
    client: httpx.AsyncClient, project: Project, leader: ProjectMember
) -> None:
    """任何项目成员可查全员摘要；响应不含密码哈希/令牌等敏感字段。"""
    leader_headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id))
    await _create_member_via_api(client, leader_headers)

    member_headers = await auth_headers(client, "alice", MEMBER_PW, project_id=str(project.id))
    resp = await client.get("/api/v1/members", headers=member_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    usernames = {m["username"] for m in body}
    assert usernames == {"leader", "alice"}
    for m in body:
        assert "password_hash" not in m
        assert "token" not in m
        assert "capabilities" in m
        assert "active_work_items" in m
        assert "weekly_available_hours" in m
    assert next(m for m in body if m["username"] == "leader")["role"] == "leader"


async def test_get_members_rejects_non_member_and_anonymous(
    client: httpx.AsyncClient, project: Project
) -> None:
    anon = await client.get("/api/v1/members")
    assert anon.status_code == 401

    async with async_session_factory() as session:
        from app.domains.identity.service import create_user

        await create_user(session, "outsider", "Outsider123!")
        await session.commit()
    outsider_headers = await auth_headers(client, "outsider", "Outsider123!", project_id=str(project.id))
    resp = await client.get("/api/v1/members", headers=outsider_headers)
    assert resp.status_code == 403
    assert resp.json()["code"] == "NOT_PROJECT_MEMBER"


async def test_capability_submit_and_confirm_flow(
    client: httpx.AsyncClient, project: Project, leader: ProjectMember
) -> None:
    """成员填报能力后未确认；负责人确认后翻转，全程留痕。"""
    leader_headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id))
    created = (await _create_member_via_api(client, leader_headers)).json()
    member_id = created["id"]
    member_headers = await auth_headers(client, "alice", MEMBER_PW, project_id=str(project.id))

    # 成员填报自己的能力 → confirmed 全部 False
    caps = {"capabilities": [{"tag": "RAG", "proficiency": 4}, {"tag": "FastAPI", "proficiency": 3}]}
    resp = await client.put(f"/api/v1/members/{member_id}/capabilities", json=caps, headers=member_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert {c["tag"] for c in body["capabilities"]} == {"RAG", "FastAPI"}
    assert all(c["confirmed"] is False for c in body["capabilities"])

    # 成员不能确认（confirm=true → 403），也不能改别人的能力
    forbidden = await client.put(
        f"/api/v1/members/{member_id}/capabilities",
        json={**caps, "confirm": True},
        headers=member_headers,
    )
    assert forbidden.status_code == 403
    other = await client.put(
        f"/api/v1/members/{leader.id}/capabilities", json=caps, headers=member_headers
    )
    assert other.status_code == 403

    # 负责人确认 → confirmed 翻转 + 记录确认人
    confirmed = await client.put(
        f"/api/v1/members/{member_id}/capabilities",
        json={**caps, "confirm": True},
        headers=leader_headers,
    )
    assert confirmed.status_code == 200
    for c in confirmed.json()["capabilities"]:
        assert c["confirmed"] is True
        assert c["confirmed_by_member_id"] == str(leader.id)
        assert c["confirmed_at"] is not None

    # 成员再次修改 → confirmed 复位为未确认（6.2 节）
    caps2 = {"capabilities": [{"tag": "RAG", "proficiency": 5}]}
    resub = await client.put(
        f"/api/v1/members/{member_id}/capabilities", json=caps2, headers=member_headers
    )
    assert resub.status_code == 200
    assert resub.json()["capabilities"][0]["confirmed"] is False

    # 审计留痕：提交与确认事件均可查
    async with async_session_factory() as session:
        events = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.action.in_(
                            ["member.capabilities.submitted", "member.capabilities.confirmed"]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
    actions = [e.action for e in events]
    assert actions.count("member.capabilities.submitted") == 2  # 首次填报 + 修改
    assert actions.count("member.capabilities.confirmed") == 1
    confirm_event = next(e for e in events if e.action == "member.capabilities.confirmed")
    assert confirm_event.actor_id == leader.user_id


async def test_leader_updates_and_disables_member(
    client: httpx.AsyncClient, project: Project, leader: ProjectMember
) -> None:
    """负责人维护资料；禁用后成员账号无法登录，启用后恢复。"""
    leader_headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id))
    created = (await _create_member_via_api(client, leader_headers)).json()
    member_id = created["id"]

    patch = await client.patch(
        f"/api/v1/members/{member_id}",
        json={"display_name": "爱丽丝", "weekly_available_hours": 20},
        headers=leader_headers,
    )
    assert patch.status_code == 200
    assert patch.json()["display_name"] == "爱丽丝"
    assert patch.json()["weekly_available_hours"] == 20

    # 禁用 → 登录被拒（users.is_active 联动）
    disabled = await client.patch(
        f"/api/v1/members/{member_id}", json={"is_active": False}, headers=leader_headers
    )
    assert disabled.status_code == 200
    assert disabled.json()["is_active"] is False
    login = await client.post(
        "/api/v1/auth/login", json={"username": "alice", "password": MEMBER_PW}
    )
    assert login.status_code == 403
    assert login.json()["code"] == "USER_DISABLED"

    # 启用 → 恢复登录
    enabled = await client.patch(
        f"/api/v1/members/{member_id}", json={"is_active": True}, headers=leader_headers
    )
    assert enabled.status_code == 200
    login = await client.post(
        "/api/v1/auth/login", json={"username": "alice", "password": MEMBER_PW}
    )
    assert login.status_code == 200

    # 普通成员不能 PATCH
    member_headers = await auth_headers(client, "alice", MEMBER_PW, project_id=str(project.id))
    resp = await client.patch(
        f"/api/v1/members/{member_id}", json={"display_name": "x"}, headers=member_headers
    )
    assert resp.status_code == 403

    # 审计：member.updated 事件含前后摘要
    async with async_session_factory() as session:
        events = (
            (await session.execute(select(AuditEvent).where(AuditEvent.action == "member.updated")))
            .scalars()
            .all()
        )
    assert len(events) >= 3
    disable_event = next(e for e in events if e.after.get("is_active") is False)
    assert disable_event.before["is_active"] is True


async def test_direct_member_creation_via_helper(
    client: httpx.AsyncClient, project: Project
) -> None:
    """add_member 辅助函数本身可用（供其他测试文件准备数据）。"""
    await add_member(project, "helper-user", "Helper123!", role="leader")
    headers = await auth_headers(client, "helper-user", "Helper123!", project_id=str(project.id))
    resp = await client.get("/api/v1/members", headers=headers)
    assert resp.status_code == 200
