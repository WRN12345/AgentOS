"""工作项的项目隔离测试。

覆盖以下行为：
- 隔离：A 项目的工作项列表不含 B 项目的工作项
- 落库归属：A 上下文创建的工作项 project_id = A
- 对象越权：A 上下文访问 B 项目工作项详情 → 404（不是 403）
- 跨实体引用：A 项目成员指派为 B 项目任务 assignee → 400
- 命令同样越权：A 上下文对 B 工作项执行命令 → 404
"""

import httpx
from sqlalchemy import select

from app.domains.project.models import Project
from app.domains.work_items.models import WorkItem
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member, auth_headers

LEADER_PW = "Leader123!"
ALICE_PW = "Alice123!"
BOB_PW = "Bob123!"


async def _setup_project(
    client: httpx.AsyncClient, project: Project, *, tag: str
) -> dict[str, object]:
    """在给定项目内准备 leader + alice + bob。

    tag 前缀区分项目（users.username 全局唯一，跨项目用户名必须不同）。
    """
    _, leader = await add_member(
        project, f"{tag}_leader", LEADER_PW, role="leader", display_name="负责人"
    )
    _, alice = await add_member(project, f"{tag}_alice", ALICE_PW, display_name="爱丽丝")
    _, bob = await add_member(project, f"{tag}_bob", BOB_PW, display_name="鲍勃")
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
        "bob_headers": await auth_headers(
            client, f"{tag}_bob", BOB_PW, project_id=str(project.id)
        ),
    }


async def _create_item(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    assignee_id: str,
    title: str = "RAG 工作项",
    **extra: object,
) -> httpx.Response:
    payload = {
        "title": title,
        "description": "实现 RAG",
        "acceptance_criteria": "评测集通过",
        "priority": "high",
        "assignee_id": assignee_id,
        "due_at": "2026-08-01T00:00:00Z",
        **extra,
    }
    return await client.post("/api/v1/work-items", json=payload, headers=headers)


async def test_create_work_item_lands_in_project(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """在项目 A 上下文中创建的工作项归属于项目 A。"""
    ctx = await _setup_project(client, project_a, tag="a")
    created = await _create_item(client, ctx["leader_headers"], str(ctx["alice"].id))  # type: ignore[union-attr]
    assert created.status_code == 201
    item_id = created.json()["id"]

    async with async_session_factory() as session:
        item = (await session.execute(select(WorkItem).where(WorkItem.id == item_id))).scalar_one()
        assert item.project_id == project_a.id


async def test_work_items_list_isolated_between_projects(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """A 的任务列表不含 B 的任务（隔离）。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")

    await _create_item(client, ctx_a["leader_headers"], str(ctx_a["alice"].id), title="任务A")  # type: ignore[union-attr]
    await _create_item(client, ctx_b["leader_headers"], str(ctx_b["alice"].id), title="任务B")  # type: ignore[union-attr]

    list_a = await client.get("/api/v1/work-items", headers=ctx_a["alice_headers"])
    assert list_a.status_code == 200
    titles_a = [i["title"] for i in list_a.json()]
    assert titles_a == ["任务A"]  # 不含 B 的任务

    list_b = await client.get("/api/v1/work-items", headers=ctx_b["alice_headers"])
    titles_b = [i["title"] for i in list_b.json()]
    assert titles_b == ["任务B"]  # 不含 A 的任务


async def test_cross_project_detail_returns_404(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """A 上下文访问 B 项目工作项详情 → 404（越权即不存在，不泄露存在性）。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")
    item_b = (
        await _create_item(client, ctx_b["leader_headers"], str(ctx_b["alice"].id), title="任务B")  # type: ignore[union-attr]
    ).json()

    resp = await client.get(f"/api/v1/work-items/{item_b['id']}", headers=ctx_a["alice_headers"])
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"

    ok = await client.get(f"/api/v1/work-items/{item_b['id']}", headers=ctx_b["alice_headers"])
    assert ok.status_code == 200


async def test_cross_project_command_returns_404(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """A 上下文对 B 项目工作项执行状态命令 → 404。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")
    item_b = (
        await _create_item(client, ctx_b["leader_headers"], str(ctx_b["alice"].id), title="任务B")  # type: ignore[union-attr]
    ).json()

    resp = await client.post(
        f"/api/v1/work-items/{item_b['id']}/publish",
        json={"version": 1},
        headers=ctx_a["leader_headers"],
    )
    assert resp.status_code == 404


async def test_cross_project_assignee_returns_400(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """把 A 项目成员指派为 B 项目任务 assignee → 400（跨实体引用校验）。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")

    resp = await _create_item(
        client, ctx_b["leader_headers"], str(ctx_a["alice"].id), title="任务B"  # type: ignore[union-attr]
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "CROSS_PROJECT_REFERENCE"


async def test_cross_project_collaborator_returns_400(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """A 项目成员作为 B 项目任务协作者 → 400。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")

    resp = await _create_item(
        client,
        ctx_b["leader_headers"],
        str(ctx_b["alice"].id),  # type: ignore[union-attr]
        title="任务B",
        collaborator_ids=[str(ctx_a["bob"].id)],  # type: ignore[union-attr]
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "CROSS_PROJECT_REFERENCE"


async def test_update_assignee_cross_project_returns_400(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """PATCH 把 assignee 改成 A 项目成员 → 400（更新路径同样校验）。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")
    item_b = (
        await _create_item(client, ctx_b["leader_headers"], str(ctx_b["alice"].id), title="任务B")  # type: ignore[union-attr]
    ).json()

    resp = await client.patch(
        f"/api/v1/work-items/{item_b['id']}",
        json={"version": 1, "assignee_id": str(ctx_a["alice"].id)},  # type: ignore[union-attr]
        headers=ctx_b["leader_headers"],
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "CROSS_PROJECT_REFERENCE"
