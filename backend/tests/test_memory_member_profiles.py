"""成员档案读写、审计、停用与跨项目可见性测试。

- 负责人创建/更新档案（upsert 直接生效），来源信息（创建/编辑者）可查；
- 非负责人写 403；目标用户不存在或不是本项目成员 404；admin 只读可读写 403；
- 项目内全员可读，含被评价者本人；跨项目成员也可读。
"""

import httpx
from sqlalchemy import select

from app.domains.audit.models import AuditEvent
from app.domains.project.models import Project
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member, auth_headers

LEADER_PW = "Leader123!"
ALICE_PW = "Alice123!"


async def _setup_users(client: httpx.AsyncClient, project: Project) -> dict:
    _, leader = await add_member(project, "leader", LEADER_PW, role="leader", display_name="负责人")
    alice_user, alice = await add_member(project, "alice", ALICE_PW, display_name="爱丽丝")
    return {
        "alice_user": alice_user,
        "alice_member": alice,
        "leader_member": leader,
        "leader_headers": await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id)),
        "alice_headers": await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id)),
    }


async def test_leader_create_and_update_profile(
    client: httpx.AsyncClient, project_a: Project
) -> None:
    ctx = await _setup_users(client, project_a)
    uid = str(ctx["alice_user"].id)

    resp = await client.put(
        f"/api/v1/memory/member-profiles/{uid}",
        headers=ctx["leader_headers"],
        json={"content": "对支付模块的历史包袱很熟"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["content"] == "对支付模块的历史包袱很熟"
    assert body["created_by"]["display_name"] == "负责人"
    assert body["last_edited_by"]["display_name"] == "负责人"

    resp = await client.put(
        f"/api/v1/memory/member-profiles/{uid}",
        headers=ctx["leader_headers"],
        json={"content": "擅长带新人"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == "擅长带新人"


async def test_non_leader_write_forbidden(
    client: httpx.AsyncClient, project_a: Project
) -> None:
    ctx = await _setup_users(client, project_a)
    resp = await client.put(
        f"/api/v1/memory/member-profiles/{ctx['alice_user'].id}",
        headers=ctx["alice_headers"],
        json={"content": "自我评价"},
    )
    assert resp.status_code == 403


async def test_write_target_outside_project_not_found(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    ctx = await _setup_users(client, project_a)
    bob_user, _ = await add_member(project_b, "bob", "Bob12345!")
    resp = await client.put(
        f"/api/v1/memory/member-profiles/{bob_user.id}",
        headers=ctx["leader_headers"],
        json={"content": "跨项目写入"},
    )
    assert resp.status_code == 404


async def test_read_visible_to_all_including_self_and_cross_project(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    ctx = await _setup_users(client, project_a)
    uid = str(ctx["alice_user"].id)
    await client.put(
        f"/api/v1/memory/member-profiles/{uid}",
        headers=ctx["leader_headers"],
        json={"content": "档案内容"},
    )

    # 被评价者本人必须可读，避免形成不可见评价。
    resp = await client.get(f"/api/v1/memory/member-profiles/{uid}", headers=ctx["alice_headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == "档案内容"

    # 档案随成员流转，是项目数据隔离的只读例外。
    _, carol = await add_member(project_b, "carol", "Carol123!")
    carol_headers = await auth_headers(client, "carol", "Carol123!", project_id=str(project_b.id))
    resp = await client.get(f"/api/v1/memory/member-profiles/{uid}", headers=carol_headers)
    assert resp.status_code == 200, resp.text


async def test_admin_readonly(client: httpx.AsyncClient, project_a: Project, admin_user) -> None:
    ctx = await _setup_users(client, project_a)
    uid = str(ctx["alice_user"].id)
    await client.put(
        f"/api/v1/memory/member-profiles/{uid}",
        headers=ctx["leader_headers"],
        json={"content": "档案内容"},
    )
    admin_headers = await auth_headers(client, "admin", "Admin123!")
    admin_headers["X-Project-Id"] = str(project_a.id)
    resp = await client.get(f"/api/v1/memory/member-profiles/{uid}", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    resp = await client.put(
        f"/api/v1/memory/member-profiles/{uid}",
        headers=admin_headers,
        json={"content": "admin 越权"},
    )
    assert resp.status_code == 403


async def test_read_missing_profile_404(client: httpx.AsyncClient, project_a: Project) -> None:
    ctx = await _setup_users(client, project_a)
    resp = await client.get(
        f"/api/v1/memory/member-profiles/{ctx['alice_user'].id}",
        headers=ctx["alice_headers"],
    )
    assert resp.status_code == 404


async def test_profile_audit_events(client: httpx.AsyncClient, project_a: Project) -> None:
    """档案创建与编辑应记录操作者、时间及前后内容。"""
    ctx = await _setup_users(client, project_a)
    uid = str(ctx["alice_user"].id)
    await client.put(
        f"/api/v1/memory/member-profiles/{uid}",
        headers=ctx["leader_headers"],
        json={"content": "初版档案"},
    )
    await client.put(
        f"/api/v1/memory/member-profiles/{uid}",
        headers=ctx["leader_headers"],
        json={"content": "修订档案"},
    )

    async with async_session_factory() as session:
        events = (
            await session.execute(
                select(AuditEvent)
                .where(AuditEvent.target_type == "member_profile")
                .order_by(AuditEvent.created_at)
            )
        ).scalars().all()
        assert [e.action for e in events] == [
            "member_profile.created",
            "member_profile.updated",
        ]
        leader = ctx["leader_member"]
        assert all(e.actor_id == leader.user_id for e in events)
        assert all(e.project_id == project_a.id for e in events)
        assert events[0].after is not None and events[0].after["content"] == "初版档案"
        assert events[1].before == {"content": "初版档案"}
        assert events[1].after == {"content": "修订档案"}
        assert all(e.created_at is not None for e in events)


async def test_deactivated_member_marked_and_excluded(
    client: httpx.AsyncClient, project_a: Project
) -> None:
    """停用成员的档案应保留并标记停用，同时从分配候选中排除。"""
    ctx = await _setup_users(client, project_a)
    uid = str(ctx["alice_user"].id)
    await client.put(
        f"/api/v1/memory/member-profiles/{uid}",
        headers=ctx["leader_headers"],
        json={"content": "档案保留"},
    )
    alice_member = ctx["alice_member"]
    resp = await client.patch(
        f"/api/v1/members/{alice_member.id}",
        headers=ctx["leader_headers"],
        json={"is_active": False},
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(
        f"/api/v1/memory/member-profiles/{uid}", headers=ctx["leader_headers"]
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == "档案保留"
    assert resp.json()["membership_active"] is False

    async with async_session_factory() as session:
        from app.agents.tools import list_assignable_members

        candidates = await list_assignable_members(session, project_id=project_a.id)
    assert str(alice_member.id) not in [c["member_id"] for c in candidates]
    assert all("爱丽丝" != c["display_name"] for c in candidates)
