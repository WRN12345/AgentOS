"""GET /approvals 聚合接口集成测试（T3.5 验收，12.6 节）。

负责人看到 PENDING 转派 + PENDING_APPROVAL DDL 变更的统一列表（按时间倒序）；
普通成员返回空列表（不 403）。
"""

import httpx

from app.domains.project.models import Project, ProjectMember
from tests.conftest import add_member, auth_headers

LEADER_PW = "Leader123!"
ALICE_PW = "Alice123!"
BOB_PW = "Bob123!"


async def _setup(client: httpx.AsyncClient, project: Project) -> dict[str, object]:
    _, leader = await add_member(project, "leader", LEADER_PW, role="leader", display_name="负责人")
    _, alice = await add_member(project, "alice", ALICE_PW, display_name="爱丽丝")
    _, bob = await add_member(project, "bob", BOB_PW, display_name="鲍勃")
    leader_headers = await auth_headers(client, "leader", LEADER_PW)
    alice_headers = await auth_headers(client, "alice", ALICE_PW)
    bob_headers = await auth_headers(client, "bob", BOB_PW)

    created = await client.post(
        "/api/v1/work-items",
        json={
            "title": "RAG 工作项",
            "description": "实现 RAG",
            "assignee_id": str(alice.id),
            "due_at": "2026-08-01T00:00:00Z",
        },
        headers=leader_headers,
    )
    assert created.status_code == 201, created.text
    item = created.json()
    published = await client.post(
        f"/api/v1/work-items/{item['id']}/publish", json={"version": 1}, headers=leader_headers
    )
    assert published.status_code == 200, published.text
    return {
        "leader": leader,
        "alice": alice,
        "bob": bob,
        "leader_headers": leader_headers,
        "alice_headers": alice_headers,
        "bob_headers": bob_headers,
        "item": published.json(),
    }


async def test_leader_sees_unified_pending_list(
    client: httpx.AsyncClient, project: Project
) -> None:
    """负责人看到两种 kind 的统一形状，含申请人/目标摘要/时间/影响分析状态，时间倒序。"""
    ctx = await _setup(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    item = ctx["item"]
    alice_headers = ctx["alice_headers"]
    leader_headers = ctx["leader_headers"]

    transfer = await client.post(
        f"/api/v1/work-items/{item['id']}/transfer-requests",  # type: ignore[index]
        json={
            "to_member_id": str(bob.id),
            "reason": "超出能力范围",
            "impact_note": "DDL 不变",
        },
        headers=alice_headers,  # type: ignore[arg-type]
    )
    assert transfer.status_code == 201, transfer.text
    deadline = await client.post(
        f"/api/v1/work-items/{item['id']}/deadline-change-requests",  # type: ignore[index]
        json={
            "target_type": "work_item",
            "target_id": item["id"],  # type: ignore[index]
            "new_due_at": "2026-08-15T00:00:00Z",
            "reason": "依赖延期",
        },
        headers=alice_headers,  # type: ignore[arg-type]
    )
    assert deadline.status_code == 201, deadline.text

    resp = await client.get("/api/v1/approvals", headers=leader_headers)  # type: ignore[arg-type]
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 2
    # 按创建时间倒序：DDL 变更在后
    assert items[0]["kind"] == "deadline_change"
    assert items[1]["kind"] == "transfer"
    assert items[0]["created_at"] >= items[1]["created_at"]

    change = items[0]
    assert change["id"] == deadline.json()["id"]
    assert change["status"] == "PENDING_APPROVAL"
    assert change["work_item_title"] == "RAG 工作项"
    assert change["requested_by"] == {"id": str(alice.id), "display_name": "爱丽丝"}
    assert change["impact_analysis_status"] == "generated"
    assert change["target_type"] == "work_item"
    assert change["target_id"] == item["id"]  # type: ignore[index]
    assert change["old_due_at"].startswith("2026-08-01")
    assert change["new_due_at"].startswith("2026-08-15")
    assert change["version"] == 1
    assert change["from_member"] is None and change["to_member"] is None

    trans = items[1]
    assert trans["id"] == transfer.json()["id"]
    assert trans["status"] == "PENDING"
    assert trans["impact_analysis_status"] is None
    assert trans["from_member"] == {"id": str(alice.id), "display_name": "爱丽丝"}
    assert trans["to_member"] == {"id": str(bob.id), "display_name": "鲍勃"}
    assert "爱丽丝" in trans["summary"] and "鲍勃" in trans["summary"]
    assert trans["target_type"] is None and trans["new_due_at"] is None

    # 审批完转派后列表只剩 DDL 变更
    approved = await client.post(
        f"/api/v1/transfer-requests/{transfer.json()['id']}/approve",
        json={"version": 1},
        headers=leader_headers,  # type: ignore[arg-type]
    )
    assert approved.status_code == 200
    resp = await client.get("/api/v1/approvals", headers=leader_headers)  # type: ignore[arg-type]
    assert [a["kind"] for a in resp.json()] == ["deadline_change"]


async def test_member_gets_empty_list(client: httpx.AsyncClient, project: Project) -> None:
    """普通成员调用 GET /approvals 返回空列表（不 403）。"""
    ctx = await _setup(client, project)
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    item = ctx["item"]
    created = await client.post(
        f"/api/v1/work-items/{item['id']}/transfer-requests",  # type: ignore[index]
        json={
            "to_member_id": str(bob.id),
            "reason": "超出能力范围",
            "impact_note": "DDL 不变",
        },
        headers=ctx["alice_headers"],  # type: ignore[arg-type]
    )
    assert created.status_code == 201

    for headers in (ctx["alice_headers"], ctx["bob_headers"]):
        resp = await client.get("/api/v1/approvals", headers=headers)  # type: ignore[arg-type]
        assert resp.status_code == 200
        assert resp.json() == []

    # 匿名 → 401
    resp = await client.get("/api/v1/approvals")
    assert resp.status_code == 401
