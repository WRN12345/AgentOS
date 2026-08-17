"""成员与能力管理集成测试（T2.3 验收，6.1、6.2、12.2、16 节）。

2026-08-17 规则调整：建号收敛到 admin（admin 控制台建号），
POST /members 仅「添加已有账号」，固定为成员角色；角色由 admin 指定/变更。
"""

import httpx
from sqlalchemy import select

from app.domains.audit.models import AuditEvent
from app.domains.identity.service import create_user
from app.domains.project.models import Project, ProjectMember
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member, auth_headers

LEADER_PW = "Leader123!"
MEMBER_PW = "Member123!"


async def test_member_cannot_add_member(
    client: httpx.AsyncClient, project: Project, leader: ProjectMember
) -> None:
    """普通成员调用 POST /members → 403；负责人可添加已有账号 → 201。"""
    leader_headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id))
    # bob 是已有全局账号（无成员记录，建号收敛到 admin）
    async with async_session_factory() as session:
        await create_user(session, "bob", MEMBER_PW)
        await session.commit()

    added = await client.post("/api/v1/members", json={"username": "bob"}, headers=leader_headers)
    assert added.status_code == 201

    _, alice = await add_member(project, "alice", MEMBER_PW)
    member_headers = await auth_headers(client, "alice", MEMBER_PW, project_id=str(project.id))
    resp = await client.post("/api/v1/members", json={"username": "bob"}, headers=member_headers)
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"


async def test_leader_adds_existing_account(
    client: httpx.AsyncClient, project: Project, leader: ProjectMember
) -> None:
    """负责人添加已有账号：不建号、无初始密码；审计留痕；账号本身可登录。"""
    leader_headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id))
    async with async_session_factory() as session:
        await create_user(session, "alice", MEMBER_PW)
        await session.commit()

    resp = await client.post(
        "/api/v1/members",
        json={"username": "alice", "display_name": "爱丽丝"},
        headers=leader_headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["role"] == "member"
    assert body["display_name"] == "爱丽丝"
    assert "initial_password" not in body
    assert "password_hash" not in body
    assert "password" not in {k for k in body}

    # 账号本身存在且可登录（建号由 admin 负责，账号可用）
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


async def test_add_member_already_in_project_409(
    client: httpx.AsyncClient, project: Project, leader: ProjectMember
) -> None:
    """重复把同一账号添加到本项目 → 409。"""
    leader_headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id))
    resp = await client.post("/api/v1/members", json={"username": "leader"}, headers=leader_headers)
    assert resp.status_code == 409
    assert resp.json()["code"] == "VALIDATION_ERROR"


async def test_get_members_returns_summary_without_sensitive_fields(
    client: httpx.AsyncClient, project: Project, leader: ProjectMember
) -> None:
    """任何项目成员可查全员摘要；响应不含密码哈希/令牌等敏感字段。"""
    leader_headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id))
    await add_member(project, "alice", MEMBER_PW)

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
    _, alice = await add_member(project, "alice", MEMBER_PW)
    member_id = alice.id
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


async def test_leader_updates_and_disables_member_project_local(
    client: httpx.AsyncClient, project: Project, leader: ProjectMember
) -> None:
    """负责人维护资料；项目内禁用仅停本项目成员身份（账号仍可登录，本项目业务 403），启用恢复。"""
    leader_headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id))
    _, alice = await add_member(project, "alice", MEMBER_PW)
    member_id = alice.id

    patch = await client.patch(
        f"/api/v1/members/{member_id}",
        json={"display_name": "爱丽丝", "weekly_available_hours": 20},
        headers=leader_headers,
    )
    assert patch.status_code == 200
    assert patch.json()["display_name"] == "爱丽丝"
    assert patch.json()["weekly_available_hours"] == 20

    # 普通成员不能 PATCH（先验，此时 alice 仍启用）
    member_headers = await auth_headers(client, "alice", MEMBER_PW, project_id=str(project.id))
    resp = await client.patch(
        f"/api/v1/members/{member_id}", json={"display_name": "x"}, headers=member_headers
    )
    assert resp.status_code == 403

    # 项目内禁用 → 仅停本项目：账号可登录（不联动 users.is_active），本项目业务 403
    disabled = await client.patch(
        f"/api/v1/members/{member_id}", json={"is_active": False}, headers=leader_headers
    )
    assert disabled.status_code == 200
    assert disabled.json()["is_active"] is False
    login = await client.post(
        "/api/v1/auth/login", json={"username": "alice", "password": MEMBER_PW}
    )
    assert login.status_code == 200  # 账号未被全局禁用
    blocked = await client.get("/api/v1/members", headers=member_headers)
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "NOT_PROJECT_MEMBER"

    # 启用 → 本项目访问恢复
    enabled = await client.patch(
        f"/api/v1/members/{member_id}", json={"is_active": True}, headers=leader_headers
    )
    assert enabled.status_code == 200
    restored_headers = await auth_headers(client, "alice", MEMBER_PW, project_id=str(project.id))
    restored = await client.get("/api/v1/members", headers=restored_headers)
    assert restored.status_code == 200

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
