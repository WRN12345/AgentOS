"""GET /approvals 聚合接口集成测试。

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
    leader_headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id))
    alice_headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id))
    bob_headers = await auth_headers(client, "bob", BOB_PW, project_id=str(project.id))

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

    resp = await client.get("/api/v1/approvals")
    assert resp.status_code == 401


async def test_processed_list_after_approve_and_reject(
    client: httpx.AsyncClient, project: Project
) -> None:
    """转派被批准、DDL 变更被驳回后：出现在 processed（含 approved_by/approved_at），
    且不再出现在 pending 列表；processed 按 updated_at 倒序。"""
    ctx = await _setup(client, project)
    leader: ProjectMember = ctx["leader"]  # type: ignore[assignment]
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    item = ctx["item"]
    leader_headers = ctx["leader_headers"]
    alice_headers = ctx["alice_headers"]

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

    # 批准转派、驳回 DDL 变更
    approved = await client.post(
        f"/api/v1/transfer-requests/{transfer.json()['id']}/approve",
        json={"version": 1},
        headers=leader_headers,  # type: ignore[arg-type]
    )
    assert approved.status_code == 200, approved.text
    rejected = await client.post(
        f"/api/v1/deadline-change-requests/{deadline.json()['id']}/reject",
        json={"version": 1},
        headers=leader_headers,  # type: ignore[arg-type]
    )
    assert rejected.status_code == 200, rejected.text

    # 不再出现在 pending 列表
    resp = await client.get("/api/v1/approvals", headers=leader_headers)  # type: ignore[arg-type]
    assert resp.status_code == 200
    assert resp.json() == []

    resp = await client.get("/api/v1/approvals/processed", headers=leader_headers)  # type: ignore[arg-type]
    assert resp.status_code == 200
    items = resp.json()
    assert [a["kind"] for a in items] == ["deadline_change", "transfer"]
    assert items[0]["updated_at"] >= items[1]["updated_at"]

    change = items[0]
    assert change["id"] == deadline.json()["id"]
    assert change["status"] == "REJECTED"
    assert change["work_item_title"] == "RAG 工作项"
    assert change["requested_by"] == {"id": str(alice.id), "display_name": "爱丽丝"}
    assert change["approved_by"] == {"id": str(leader.id), "display_name": "负责人"}
    assert change["approved_at"] is not None

    trans = items[1]
    assert trans["id"] == transfer.json()["id"]
    assert trans["status"] == "APPROVED"
    assert trans["from_member"] == {"id": str(alice.id), "display_name": "爱丽丝"}
    assert trans["to_member"] == {"id": str(bob.id), "display_name": "鲍勃"}
    assert trans["approved_by"] == {"id": str(leader.id), "display_name": "负责人"}
    assert trans["approved_at"] is not None


async def test_processed_list_member_gets_empty_list(
    client: httpx.AsyncClient, project: Project
) -> None:
    """普通成员调用 GET /approvals/processed 返回空列表（不 403）；匿名 401。"""
    ctx = await _setup(client, project)
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    item = ctx["item"]

    transfer = await client.post(
        f"/api/v1/work-items/{item['id']}/transfer-requests",  # type: ignore[index]
        json={
            "to_member_id": str(bob.id),
            "reason": "超出能力范围",
            "impact_note": "DDL 不变",
        },
        headers=ctx["alice_headers"],  # type: ignore[arg-type]
    )
    assert transfer.status_code == 201, transfer.text
    approved = await client.post(
        f"/api/v1/transfer-requests/{transfer.json()['id']}/approve",
        json={"version": 1},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert approved.status_code == 200, approved.text

    for headers in (ctx["alice_headers"], ctx["bob_headers"]):
        resp = await client.get("/api/v1/approvals/processed", headers=headers)  # type: ignore[arg-type]
        assert resp.status_code == 200
        assert resp.json() == []

    resp = await client.get("/api/v1/approvals/processed")
    assert resp.status_code == 401


async def test_processed_list_includes_delivery_review(
    client: httpx.AsyncClient, project: Project
) -> None:
    """交付审核结论进入 processed（kind=delivery_review）：requested_by 为交付物
    提交人、status 为审核结论、approved_by/at 为负责人与处理时间、含交付物版本。"""
    ctx = await _setup(client, project)
    leader: ProjectMember = ctx["leader"]  # type: ignore[assignment]
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    item = ctx["item"]
    leader_headers = ctx["leader_headers"]
    alice_headers = ctx["alice_headers"]

    resp = await client.post(
        f"/api/v1/work-items/{item['id']}/dev-doc/waive",  # type: ignore[index]
        json={},
        headers=leader_headers,  # type: ignore[arg-type]
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"/api/v1/work-items/{item['id']}/start",  # type: ignore[index]
        json={"version": 2},
        headers=alice_headers,  # type: ignore[arg-type]
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"/api/v1/work-items/{item['id']}/deliverables",  # type: ignore[index]
        json={"type": "text", "content": "交付说明"},
        headers=alice_headers,  # type: ignore[arg-type]
    )
    assert resp.status_code == 201, resp.text
    deliverable_id = resp.json()["id"]
    resp = await client.post(
        f"/api/v1/work-items/{item['id']}/submit",  # type: ignore[index]
        json={"version": 3},
        headers=alice_headers,  # type: ignore[arg-type]
    )
    assert resp.status_code == 200, resp.text

    reviewed = await client.post(
        f"/api/v1/work-items/{item['id']}/reviews",  # type: ignore[index]
        json={"deliverable_id": deliverable_id, "decision": "approve", "feedback": "可以"},
        headers=leader_headers,  # type: ignore[arg-type]
    )
    assert reviewed.status_code == 201, reviewed.text

    resp = await client.get("/api/v1/approvals/processed", headers=leader_headers)  # type: ignore[arg-type]
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    entry = items[0]
    assert entry["kind"] == "delivery_review"
    assert entry["status"] == "approve"
    assert entry["work_item_title"] == "RAG 工作项"
    assert entry["deliverable_version"] == 1
    assert entry["deliverable_type"] == "text"
    assert entry["requested_by"] == {"id": str(alice.id), "display_name": "爱丽丝"}
    assert entry["approved_by"] == {"id": str(leader.id), "display_name": "负责人"}
    assert entry["approved_at"] is not None
    # 审批聚合不返回反馈正文，避免扩大敏感内容的暴露范围。
    assert "feedback" not in entry
