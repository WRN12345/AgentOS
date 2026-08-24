"""成员档案 CRUD 与权限测试（M3.5 验收，设计文档第 7 节②、16.1）。

- 负责人创建/更新档案（upsert 直接生效），来源信息（创建/编辑者）可查；
- 非负责人写 403；目标用户不存在或不是本项目成员 404；admin 只读可读写 403；
- 项目内全员可读，含被评价者本人（16.1）；跨项目成员也可读（随人走）。
"""

import httpx

from app.domains.project.models import Project
from tests.conftest import add_member, auth_headers

LEADER_PW = "Leader123!"
ALICE_PW = "Alice123!"


async def _setup_users(client: httpx.AsyncClient, project: Project) -> dict:
    _, leader = await add_member(project, "leader", LEADER_PW, role="leader", display_name="负责人")
    alice_user, alice = await add_member(project, "alice", ALICE_PW, display_name="爱丽丝")
    return {
        "alice_user": alice_user,
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

    # 更新：内容替换，编辑者留痕
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
    # A 项目负责人给 B 项目成员写档案 → 404（目标不是本项目成员）
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

    # 本人可读（16.1，不做暗箱评价）
    resp = await client.get(f"/api/v1/memory/member-profiles/{uid}", headers=ctx["alice_headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == "档案内容"

    # 他项目成员也可读（档案随人走、跨项目可见的唯一例外）
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
    # admin 无项目成员身份，编辑 403（第 12 节）
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
