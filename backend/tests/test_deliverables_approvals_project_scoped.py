"""多项目改造 ticket 03：交付物与审批项目隔离测试（主接缝，HTTP API 层）。

覆盖行为矩阵（spec Testing Decisions / ticket 03 验收）：
- 落库归属：A 上下文提交的交付物 project_id = A（经所属工作项推导填充）
- 列表隔离：交付物聚合页、审批待办、我的转派申请只含当前项目
- 对象越权：A 上下文访问 B 项目交付物/转派/开发文档/评审 → 404（不是 403）
- 审批操作越权：A 负责人审批 B 项目转派/DDL 变更 → 404
- 跨实体引用：A 项目成员作为 B 项目协作接收人 → 400（CROSS_PROJECT_REFERENCE）
"""

import httpx
from sqlalchemy import select

from app.domains.deliverables.models import Deliverable
from app.domains.project.models import Project
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member, add_member_for_existing_user, auth_headers

LEADER_PW = "Leader123!"
ALICE_PW = "Alice123!"
BOB_PW = "Bob123!"


async def _setup_project(
    client: httpx.AsyncClient, project: Project, *, tag: str
) -> dict[str, object]:
    """在给定项目内准备 leader + alice + bob（tag 前缀区分项目）。"""
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
) -> httpx.Response:
    payload = {
        "title": title,
        "description": "实现 RAG",
        "acceptance_criteria": "评测集通过",
        "priority": "high",
        "assignee_id": assignee_id,
        "due_at": "2026-08-01T00:00:00Z",
    }
    return await client.post("/api/v1/work-items", json=payload, headers=headers)


async def _submit_deliverable(
    client: httpx.AsyncClient, headers: dict[str, str], item_id: str
) -> httpx.Response:
    return await client.post(
        f"/api/v1/work-items/{item_id}/deliverables",
        json={"type": "text", "content": "交付物正文"},
        headers=headers,
    )


async def _request_transfer(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    item_id: str,
    to_member_id: str,
) -> httpx.Response:
    return await client.post(
        f"/api/v1/work-items/{item_id}/transfer-requests",
        json={
            "to_member_id": to_member_id,
            "reason": "调整负责人",
            "impact_note": "进行中的协作请求由新负责人接管",
        },
        headers=headers,
    )


# ---------- 交付物 ----------


async def test_deliverable_lands_in_project(
    client: httpx.AsyncClient, project_a: Project
) -> None:
    """A 上下文提交的交付物落库 project_id = A（spec D1：经所属工作项推导填充）。"""
    ctx = await _setup_project(client, project_a, tag="a")
    item = (await _create_item(client, ctx["leader_headers"], str(ctx["alice"].id))).json()  # type: ignore[union-attr]
    resp = await _submit_deliverable(client, ctx["alice_headers"], item["id"])  # type: ignore[arg-type]
    assert resp.status_code == 201
    deliverable_id = resp.json()["id"]

    async with async_session_factory() as session:
        d = (
            await session.execute(select(Deliverable).where(Deliverable.id == deliverable_id))
        ).scalar_one()
        assert d.project_id == project_a.id


async def test_deliverables_aggregate_isolated_between_projects(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """交付物聚合页：A 上下文只见 A 项目的交付物，不见 B 的。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")
    item_a = (await _create_item(client, ctx_a["leader_headers"], str(ctx_a["alice"].id), title="任务A")).json()  # type: ignore[union-attr]
    item_b = (await _create_item(client, ctx_b["leader_headers"], str(ctx_b["alice"].id), title="任务B")).json()  # type: ignore[union-attr]
    await _submit_deliverable(client, ctx_a["alice_headers"], item_a["id"])  # type: ignore[arg-type]
    await _submit_deliverable(client, ctx_b["alice_headers"], item_b["id"])  # type: ignore[arg-type]

    list_a = await client.get("/api/v1/deliverables", headers=ctx_a["leader_headers"])  # type: ignore[arg-type]
    assert list_a.status_code == 200
    titles_a = [d["work_item_title"] for d in list_a.json()]
    assert "任务A" in titles_a
    assert "任务B" not in titles_a  # 不泄露 B 项目交付物

    list_b = await client.get("/api/v1/deliverables", headers=ctx_b["leader_headers"])  # type: ignore[arg-type]
    titles_b = [d["work_item_title"] for d in list_b.json()]
    assert "任务B" in titles_b
    assert "任务A" not in titles_b


async def test_cross_project_deliverable_submit_returns_404(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """A 上下文向 B 项目工作项提交交付物 → 404（越权即不存在）。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")
    item_b = (
        await _create_item(client, ctx_b["leader_headers"], str(ctx_b["alice"].id), title="任务B")  # type: ignore[union-attr]
    ).json()

    resp = await _submit_deliverable(client, ctx_a["leader_headers"], item_b["id"])  # type: ignore[arg-type]
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


async def test_cross_project_deliverable_list_returns_404(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """A 上下文查 B 项目工作项的交付物版本历史 → 404。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")
    item_b = (
        await _create_item(client, ctx_b["leader_headers"], str(ctx_b["alice"].id), title="任务B")  # type: ignore[union-attr]
    ).json()

    resp = await client.get(
        f"/api/v1/work-items/{item_b['id']}/deliverables", headers=ctx_a["leader_headers"]  # type: ignore[arg-type]
    )
    assert resp.status_code == 404


# ---------- 转派 ----------


async def test_transfer_mine_isolated_for_same_user_across_projects(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """同一用户跨 A/B 项目各发起转派，list_mine 只含当前项目（spec D2 经工作项推导）。"""
    from app.domains.identity.models import User

    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")

    # 把 A 的 alice 账号也加进 B 项目（同一 User，两个 member 记录）
    async with async_session_factory() as session:
        user = await session.get(User, ctx_a["alice"].user_id)  # type: ignore[union-attr]
    member_b = await add_member_for_existing_user(
        async_session_factory, project_b, user, role="member", display_name="爱丽丝-B"
    )
    headers_b = await auth_headers(client, "a_alice", ALICE_PW, project_id=str(project_b.id))

    item_a = (
        await _create_item(client, ctx_a["leader_headers"], str(ctx_a["alice"].id), title="任务A")  # type: ignore[union-attr]
    ).json()
    item_b = (
        await _create_item(client, ctx_b["leader_headers"], str(member_b.id), title="任务B")
    ).json()

    req_a = await _request_transfer(client, ctx_a["alice_headers"], item_a["id"], str(ctx_a["bob"].id))  # type: ignore[arg-type,union-attr]
    assert req_a.status_code == 201
    req_b = await _request_transfer(client, headers_b, item_b["id"], str(ctx_b["bob"].id))
    assert req_b.status_code == 201

    mine_a = await client.get("/api/v1/transfer-requests?role=mine", headers=ctx_a["alice_headers"])  # type: ignore[arg-type]
    mine_b = await client.get("/api/v1/transfer-requests?role=mine", headers=headers_b)
    assert mine_a.status_code == 200
    assert mine_b.status_code == 200
    assert [t["work_item_title"] for t in mine_a.json()] == ["任务A"]
    assert [t["work_item_title"] for t in mine_b.json()] == ["任务B"]


async def test_cross_project_transfer_detail_returns_404(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """A 上下文读 B 项目转派申请详情 → 404；同项目正常可见。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")
    item_b = (
        await _create_item(client, ctx_b["leader_headers"], str(ctx_b["bob"].id), title="任务B")  # type: ignore[union-attr]
    ).json()
    req = await _request_transfer(
        client, ctx_b["bob_headers"], item_b["id"], str(ctx_b["alice"].id)  # type: ignore[arg-type,union-attr]
    )
    assert req.status_code == 201
    request_id = req.json()["id"]

    resp = await client.get(f"/api/v1/transfer-requests/{request_id}", headers=ctx_a["leader_headers"])  # type: ignore[arg-type]
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"

    ok = await client.get(f"/api/v1/transfer-requests/{request_id}", headers=ctx_b["leader_headers"])  # type: ignore[arg-type]
    assert ok.status_code == 200


async def test_cross_project_transfer_approve_returns_404(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """A 负责人审批 B 项目转派申请 → 404（审批操作校验工作项同项目）。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")
    item_b = (
        await _create_item(client, ctx_b["leader_headers"], str(ctx_b["bob"].id), title="任务B")  # type: ignore[union-attr]
    ).json()
    req = await _request_transfer(
        client, ctx_b["bob_headers"], item_b["id"], str(ctx_b["alice"].id)  # type: ignore[arg-type,union-attr]
    )
    request_id = req.json()["id"]

    resp = await client.post(
        f"/api/v1/transfer-requests/{request_id}/approve",
        json={"version": 1},
        headers=ctx_a["leader_headers"],  # type: ignore[arg-type]
    )
    assert resp.status_code == 404


# ---------- 协作 ----------


async def test_cross_project_collaboration_assignee_returns_400(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """A 项目成员作为 B 项目协作接收人 → 400（跨实体引用校验，spec D3）。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")
    item_b = (
        await _create_item(client, ctx_b["leader_headers"], str(ctx_b["alice"].id), title="任务B")  # type: ignore[union-attr]
    ).json()

    resp = await client.post(
        f"/api/v1/work-items/{item_b['id']}/collaboration-requests",
        json={
            "title": "协作",
            "goal": "目标",
            "assignee_id": str(ctx_a["bob"].id),  # type: ignore[union-attr]
        },
        headers=ctx_b["alice_headers"],  # type: ignore[arg-type]
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "CROSS_PROJECT_REFERENCE"


# ---------- 开发文档 / 评审 ----------


async def test_cross_project_dev_doc_returns_404(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """A 上下文查 B 项目工作项的开发文档 → 404。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")
    item_b = (
        await _create_item(client, ctx_b["leader_headers"], str(ctx_b["alice"].id), title="任务B")  # type: ignore[union-attr]
    ).json()

    resp = await client.get(f"/api/v1/work-items/{item_b['id']}/dev-doc", headers=ctx_a["leader_headers"])  # type: ignore[arg-type]
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


async def test_cross_project_reviews_returns_404(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """A 上下文查 B 项目工作项的审核记录 → 404。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")
    item_b = (
        await _create_item(client, ctx_b["leader_headers"], str(ctx_b["alice"].id), title="任务B")  # type: ignore[union-attr]
    ).json()

    resp = await client.get(f"/api/v1/work-items/{item_b['id']}/reviews", headers=ctx_a["leader_headers"])  # type: ignore[arg-type]
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


# ---------- 审批聚合 ----------


async def test_approvals_pending_isolated_between_projects(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """审批聚合页：A 负责人待办不含 B 项目转派申请；B 负责人可见自己的。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")
    item_b = (
        await _create_item(client, ctx_b["leader_headers"], str(ctx_b["bob"].id), title="任务B")  # type: ignore[union-attr]
    ).json()
    req = await _request_transfer(
        client, ctx_b["bob_headers"], item_b["id"], str(ctx_b["alice"].id)  # type: ignore[arg-type,union-attr]
    )
    assert req.status_code == 201

    pending_a = await client.get("/api/v1/approvals", headers=ctx_a["leader_headers"])  # type: ignore[arg-type]
    assert pending_a.status_code == 200
    assert pending_a.json() == []  # B 的待审批不可见

    pending_b = await client.get("/api/v1/approvals", headers=ctx_b["leader_headers"])  # type: ignore[arg-type]
    assert pending_b.status_code == 200
    assert len(pending_b.json()) == 1
    assert pending_b.json()[0]["kind"] == "transfer"


async def test_cross_project_deadline_change_approve_returns_404(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """A 负责人审批 B 项目 DDL 变更申请 → 404。"""
    ctx_a = await _setup_project(client, project_a, tag="a")
    ctx_b = await _setup_project(client, project_b, tag="b")
    item_b = (
        await _create_item(client, ctx_b["leader_headers"], str(ctx_b["bob"].id), title="任务B")  # type: ignore[union-attr]
    ).json()

    created = await client.post(
        f"/api/v1/work-items/{item_b['id']}/deadline-change-requests",
        json={
            "target_type": "work_item",
            "target_id": item_b["id"],
            "new_due_at": "2026-08-10T00:00:00Z",
            "reason": "依赖方延期，需要顺延",
        },
        headers=ctx_b["bob_headers"],  # type: ignore[arg-type]
    )
    assert created.status_code == 201, created.text
    request_id = created.json()["id"]

    resp = await client.post(
        f"/api/v1/deadline-change-requests/{request_id}/approve",
        json={"version": 1},
        headers=ctx_a["leader_headers"],  # type: ignore[arg-type]
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"
