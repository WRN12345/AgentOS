"""核心记忆条目 API 契约测试（M4.3 验收，设计文档第 8、12 节）。

- 列表/创建/作废三接口按项目隔离，跨项目访问 404/403；
- 项目成员可读（含来源信息：谁提的、谁确认的、何时生效），全局 admin 只读；
- 写操作仅负责人：创建 201 立即生效，超容量预算 400，作废后状态 deprecated。
"""

import asyncio

import httpx

from app.domains.memory.core_memory import budget_usage
from app.domains.project.models import Project, ProjectMember
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member, auth_headers

LEADER_PW = "Leader123!"


async def _create(
    client: httpx.AsyncClient, headers: dict[str, str], content: str
) -> httpx.Response:
    return await client.post(
        "/api/v1/memory/core-entries", headers=headers, json={"content": content}
    )


async def test_member_can_list_with_source_info(
    client: httpx.AsyncClient, project_a: Project, leader: ProjectMember
) -> None:
    _, member = await add_member(project_a, "dave", "Dave12345!")
    leader_headers = await auth_headers(
        client, "leader", LEADER_PW, project_id=str(project_a.id)
    )
    resp = await _create(client, leader_headers, "本项目禁用递归查询")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "active"
    assert body["scope"] == "project"
    assert body["proposed_by"]["display_name"] == "负责人"
    assert body["confirmed_by"]["display_name"] == "负责人"
    assert body["effective_at"] is not None

    member_headers = await auth_headers(
        client, "dave", "Dave12345!", project_id=str(project_a.id)
    )
    resp = await client.get("/api/v1/memory/core-entries", headers=member_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [e["id"] for e in data["entries"]] == [body["id"]]
    assert data["used_chars"] == len("本项目禁用递归查询")
    assert data["budget_chars"] == 4000


async def test_list_project_isolation(
    client: httpx.AsyncClient,
    project_a: Project,
    project_b: Project,
    leader: ProjectMember,
) -> None:
    _, leader_b = await add_member(project_b, "leaderb", "LeaderB123!", role="leader")
    leader_headers_a = await auth_headers(
        client, "leader", LEADER_PW, project_id=str(project_a.id)
    )
    await _create(client, leader_headers_a, "A 项目约定")

    leader_headers_b = await auth_headers(
        client, "leaderb", "LeaderB123!", project_id=str(project_b.id)
    )
    resp = await client.get("/api/v1/memory/core-entries", headers=leader_headers_b)
    assert resp.status_code == 200, resp.text
    assert resp.json()["entries"] == []

    # A 项目负责人带 B 项目上下文 → 403（不是 B 项目成员）
    resp = await client.get(
        "/api/v1/memory/core-entries",
        headers={"Authorization": leader_headers_a["Authorization"], "X-Project-Id": str(project_b.id)},
    )
    assert resp.status_code == 403


async def test_admin_readonly(
    client: httpx.AsyncClient, project_a: Project, leader: ProjectMember, admin_user
) -> None:
    leader_headers = await auth_headers(
        client, "leader", LEADER_PW, project_id=str(project_a.id)
    )
    await _create(client, leader_headers, "约定")

    admin_headers = await auth_headers(client, "admin", "Admin123!")
    admin_headers["X-Project-Id"] = str(project_a.id)
    resp = await client.get("/api/v1/memory/core-entries", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["entries"]) == 1
    # admin 只读：无项目成员身份，写操作 403
    resp = await _create(client, admin_headers, "admin 越权写")
    assert resp.status_code == 403


async def test_create_requires_leader(
    client: httpx.AsyncClient, project_a: Project, leader: ProjectMember
) -> None:
    await add_member(project_a, "erin", "Erin12345!")
    member_headers = await auth_headers(
        client, "erin", "Erin12345!", project_id=str(project_a.id)
    )
    resp = await _create(client, member_headers, "成员越权写")
    assert resp.status_code == 403


async def test_budget_exceeded_contract(
    client: httpx.AsyncClient, project_a: Project, leader: ProjectMember
) -> None:
    headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project_a.id))
    resp = await _create(client, headers, "x" * 3990)
    assert resp.status_code == 201, resp.text
    resp = await _create(client, headers, "y" * 20)
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "CORE_MEMORY_BUDGET_EXCEEDED"
    assert body["details"]["budget"] == 4000


async def test_concurrent_creates_cannot_exceed_budget(
    client: httpx.AsyncClient, project_a: Project, leader: ProjectMember
) -> None:
    """项目预算锁应使并发创建中只有一条基于空预算成功。"""
    headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project_a.id))

    responses = await asyncio.gather(
        _create(client, headers, "x" * 2500),
        _create(client, headers, "y" * 2500),
    )

    assert sorted(response.status_code for response in responses) == [201, 400]
    rejected = next(response for response in responses if response.status_code == 400)
    assert rejected.json()["code"] == "CORE_MEMORY_BUDGET_EXCEEDED"
    async with async_session_factory() as session:
        used, budget = await budget_usage(session, project_id=project_a.id)
    assert used == 2500
    assert budget == 4000


async def test_deprecate_flow_and_isolation(
    client: httpx.AsyncClient,
    project_a: Project,
    project_b: Project,
    leader: ProjectMember,
) -> None:
    await add_member(project_a, "frank", "Frank123!")
    await add_member(project_b, "leaderb", "LeaderB123!", role="leader")
    headers_a = await auth_headers(client, "leader", LEADER_PW, project_id=str(project_a.id))
    entry_id = (await _create(client, headers_a, "将被作废的约定")).json()["id"]

    # 非负责人作废 → 403
    member_headers = await auth_headers(
        client, "frank", "Frank123!", project_id=str(project_a.id)
    )
    resp = await client.post(
        f"/api/v1/memory/core-entries/{entry_id}/deprecate", headers=member_headers
    )
    assert resp.status_code == 403

    # 他项目负责人作废 → 404（跨项目按不存在处理）
    headers_b = await auth_headers(client, "leaderb", "LeaderB123!", project_id=str(project_b.id))
    resp = await client.post(
        f"/api/v1/memory/core-entries/{entry_id}/deprecate", headers=headers_b
    )
    assert resp.status_code == 404

    # 正常作废 → deprecated；重复作废 → 409
    resp = await client.post(
        f"/api/v1/memory/core-entries/{entry_id}/deprecate", headers=headers_a
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "deprecated"
    resp = await client.post(
        f"/api/v1/memory/core-entries/{entry_id}/deprecate", headers=headers_a
    )
    assert resp.status_code == 409

    # 作废条目仍可在列表追溯
    resp = await client.get("/api/v1/memory/core-entries", headers=headers_a)
    assert resp.json()["entries"][0]["status"] == "deprecated"


async def test_missing_project_header(
    client: httpx.AsyncClient, project_a: Project, leader: ProjectMember
) -> None:
    headers = await auth_headers(client, "leader", LEADER_PW)  # 不带 X-Project-Id
    resp = await client.get("/api/v1/memory/core-entries", headers=headers)
    assert resp.status_code == 400
