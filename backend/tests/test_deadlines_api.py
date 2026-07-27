"""DDL 变更申请 API 集成测试（T3.4 验收，7.4、8.4、12.4、16、17.2 节）。"""

import uuid

import httpx
import pytest
from sqlalchemy import select

from app.domains.audit.models import AuditEvent
from app.domains.notifications.models import Notification
from app.domains.project.models import Project, ProjectMember
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member, auth_headers

LEADER_PW = "Leader123!"
ALICE_PW = "Alice123!"
BOB_PW = "Bob123!"


async def _setup(client: httpx.AsyncClient, project: Project) -> dict[str, object]:
    """准备：leader + alice（主执行人）+ bob（协作接收人）；工作项 DDL 2026-08-01，
    协作请求（alice → bob）DDL 2026-07-30。"""
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

    collab = await client.post(
        f"/api/v1/work-items/{item['id']}/collaboration-requests",
        json={
            "assignee_id": str(bob.id),
            "title": "标注训练样本",
            "goal": "完成 100 条样本标注",
            "due_at": "2026-07-30T00:00:00Z",
        },
        headers=alice_headers,
    )
    assert collab.status_code == 201, collab.text
    return {
        "leader": leader,
        "alice": alice,
        "bob": bob,
        "leader_headers": leader_headers,
        "alice_headers": alice_headers,
        "bob_headers": bob_headers,
        "item": published.json(),
        "collab": collab.json(),
    }


async def _create_change(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    item_id: str,
    target_type: str,
    target_id: str,
    new_due_at: str,
) -> httpx.Response:
    return await client.post(
        f"/api/v1/work-items/{item_id}/deadline-change-requests",
        json={
            "target_type": target_type,
            "target_id": target_id,
            "new_due_at": new_due_at,
            "reason": "依赖方延期，需要顺延",
        },
        headers=headers,
    )


async def _audit_actions(target_id: str) -> list[str]:
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(AuditEvent.action)
                .where(AuditEvent.target_id == uuid.UUID(target_id))
                .order_by(AuditEvent.created_at)
            )
        ).scalars().all()
    return list(rows)


async def _notifications_of(member_id: uuid.UUID) -> list[Notification]:
    async with async_session_factory() as session:
        return list(
            (
                await session.execute(
                    select(Notification)
                    .where(Notification.recipient_id == member_id)
                    .order_by(Notification.created_at)
                )
            )
            .scalars()
            .all()
        )


async def _work_item(client: httpx.AsyncClient, headers: dict[str, str], item_id: str) -> dict:
    resp = await client.get(f"/api/v1/work-items/{item_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _collab(client: httpx.AsyncClient, headers: dict[str, str], item_id: str, collab_id: str) -> dict:
    resp = await client.get(
        f"/api/v1/work-items/{item_id}/collaboration-requests", headers=headers
    )
    assert resp.status_code == 200, resp.text
    return next(r for r in resp.json() if r["id"] == collab_id)


# ---------- 协作级：不影响主任务 DDL → 自动生效（7.4 节） ----------


async def test_collab_level_auto_approved(client: httpx.AsyncClient, project: Project) -> None:
    """新协作 DDL ≤ 主任务 DDL：同事务直接生效，无需负责人，审计留痕。"""
    ctx = await _setup(client, project)
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    leader: ProjectMember = ctx["leader"]  # type: ignore[assignment]
    item = ctx["item"]
    collab = ctx["collab"]
    alice_headers = ctx["alice_headers"]

    created = await _create_change(
        client, alice_headers, item["id"], "collaboration_request", collab["id"], "2026-07-31T00:00:00Z"  # type: ignore[arg-type,index]
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "APPROVED"
    assert body["impact_analysis_status"] == "generated"
    assert body["old_due_at"].startswith("2026-07-30")
    assert body["new_due_at"].startswith("2026-07-31")
    assert body["approved_by"] is not None

    # 规则化影响分析内容
    analysis = body["impact_analysis"]
    assert analysis["exceeds_work_item_due"] is False
    assert analysis["work_item"]["due_at"].startswith("2026-08-01")
    affected_ids = {c["id"] for c in analysis["affected_collaboration_requests"]}
    assert collab["id"] in affected_ids  # type: ignore[index]

    # 协作 DDL 同事务更新且 version+1
    updated = await _collab(client, alice_headers, item["id"], collab["id"])  # type: ignore[arg-type,index]
    assert updated["due_at"].startswith("2026-07-31")
    assert updated["version"] == 2  # type: ignore[index] collab 创建为 1

    # 审计：申请 + 自动生效留痕
    assert await _audit_actions(body["id"]) == [
        "deadline_change.requested",
        "deadline_change.approved",
    ]
    async with async_session_factory() as session:
        approved_event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.target_id == uuid.UUID(body["id"]),
                    AuditEvent.action == "deadline_change.approved",
                )
            )
        ).scalar_one()
    assert approved_event.after["auto_approved"] is True

    # 协作对端收到通知；负责人无待审批（不在 GET /approvals 中）
    bob_types = [n.type for n in await _notifications_of(bob.id)]
    assert "deadline_change.approved" in bob_types
    leader_types = [n.type for n in await _notifications_of(leader.id)]
    assert "deadline_change.requested" not in leader_types
    approvals = await client.get("/api/v1/approvals", headers=ctx["leader_headers"])  # type: ignore[arg-type]
    assert approvals.json() == []


async def test_collab_level_exceeds_goes_to_leader_approval(
    client: httpx.AsyncClient, project: Project
) -> None:
    """新协作 DDL 晚于主任务 DDL：走负责人审批流；批准前协作 DDL 不变。"""
    ctx = await _setup(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    item = ctx["item"]
    collab = ctx["collab"]
    alice_headers = ctx["alice_headers"]

    created = await _create_change(
        client, alice_headers, item["id"], "collaboration_request", collab["id"], "2026-08-10T00:00:00Z"  # type: ignore[arg-type,index]
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "PENDING_APPROVAL"
    assert body["impact_analysis"]["exceeds_work_item_due"] is True

    # 批准前协作 DDL 不变
    before = await _collab(client, alice_headers, item["id"], collab["id"])  # type: ignore[arg-type,index]
    assert before["due_at"].startswith("2026-07-30")

    # 负责人在 approvals 看到（kind=deadline_change，含 impact_analysis_status）
    approvals = await client.get("/api/v1/approvals", headers=ctx["leader_headers"])  # type: ignore[arg-type]
    entries = [a for a in approvals.json() if a["kind"] == "deadline_change"]
    assert [a["id"] for a in entries] == [body["id"]]
    assert entries[0]["impact_analysis_status"] == "generated"
    assert entries[0]["target_type"] == "collaboration_request"

    approved = await client.post(
        f"/api/v1/deadline-change-requests/{body['id']}/approve",
        json={"version": 1, "decision_note": "同意顺延"},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"

    after = await _collab(client, alice_headers, item["id"], collab["id"])  # type: ignore[arg-type,index]
    assert after["due_at"].startswith("2026-08-10")
    assert after["version"] == 2

    assert await _audit_actions(body["id"]) == [
        "deadline_change.requested",
        "deadline_change.approved",
    ]
    alice_types = [n.type for n in await _notifications_of(alice.id)]
    assert "deadline_change.approved" in alice_types


# ---------- 主任务级：一律负责人审批（7.4 节） ----------


async def test_main_level_requires_leader_approval(
    client: httpx.AsyncClient, project: Project
) -> None:
    """主任务 DDL 未经批准不生效；同工作项重复发起 → 409；批准后同事务更新 + 审计。"""
    ctx = await _setup(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    item = ctx["item"]
    alice_headers = ctx["alice_headers"]
    leader_headers = ctx["leader_headers"]

    created = await _create_change(
        client, alice_headers, item["id"], "work_item", item["id"], "2026-08-15T00:00:00Z"  # type: ignore[arg-type,index]
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "PENDING_APPROVAL"
    assert body["target_type"] == "work_item"

    # 未经批准：主任务 DDL 不变
    before = await _work_item(client, alice_headers, item["id"])  # type: ignore[arg-type,index]
    assert before["due_at"].startswith("2026-08-01")

    # 同一工作项只能有一个待审批主 DDL 变更（17.2 节）
    dup = await _create_change(
        client, alice_headers, item["id"], "work_item", item["id"], "2026-08-20T00:00:00Z"  # type: ignore[arg-type,index]
    )
    assert dup.status_code == 409
    assert dup.json()["code"] == "DEADLINE_CHANGE_PENDING_CONFLICT"

    approved = await client.post(
        f"/api/v1/deadline-change-requests/{body['id']}/approve",
        json={"version": 1},
        headers=leader_headers,  # type: ignore[arg-type]
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"

    # 同事务：工作项 DDL 更新 + version+1（publish 后 2 → 3）+ 审计
    after = await _work_item(client, alice_headers, item["id"])  # type: ignore[arg-type,index]
    assert after["due_at"].startswith("2026-08-15")
    assert after["version"] == 3
    assert await _audit_actions(body["id"]) == [
        "deadline_change.requested",
        "deadline_change.approved",
    ]
    alice_types = [n.type for n in await _notifications_of(alice.id)]
    assert "deadline_change.approved" in alice_types

    # 批准后可发起新的主 DDL 变更
    again = await _create_change(
        client, alice_headers, item["id"], "work_item", item["id"], "2026-08-20T00:00:00Z"  # type: ignore[arg-type,index]
    )
    assert again.status_code == 201


async def test_main_level_reject_keeps_due(client: httpx.AsyncClient, project: Project) -> None:
    """负责人驳回主 DDL 变更：工作项 DDL 不变，发起人收到驳回通知。"""
    ctx = await _setup(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    item = ctx["item"]
    alice_headers = ctx["alice_headers"]

    created = await _create_change(
        client, alice_headers, item["id"], "work_item", item["id"], "2026-08-15T00:00:00Z"  # type: ignore[arg-type,index]
    )
    body = created.json()
    rejected = await client.post(
        f"/api/v1/deadline-change-requests/{body['id']}/reject",
        json={"version": 1, "decision_note": "按原计划执行"},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "REJECTED"

    after = await _work_item(client, alice_headers, item["id"])  # type: ignore[arg-type,index]
    assert after["due_at"].startswith("2026-08-01")
    assert after["version"] == 2

    alice_notifications = [
        n for n in await _notifications_of(alice.id) if n.type == "deadline_change.rejected"
    ]
    assert len(alice_notifications) == 1
    assert "按原计划" not in alice_notifications[0].body  # 意见不进通知（16 节）


# ---------- 影响分析失败不阻塞审批（8.4 节） ----------


async def test_impact_analysis_unavailable_still_approvable(
    client: httpx.AsyncClient, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模拟影响分析异常：状态照常推进到 PENDING_APPROVAL，标识 unavailable，
    负责人仍可走完审批，响应带明确标识。"""
    import app.domains.deadlines.service as deadlines_service

    async def _boom(**kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("analysis backend down")

    monkeypatch.setattr(deadlines_service, "generate_impact_analysis", _boom)

    ctx = await _setup(client, project)
    item = ctx["item"]
    alice_headers = ctx["alice_headers"]

    created = await _create_change(
        client, alice_headers, item["id"], "work_item", item["id"], "2026-08-15T00:00:00Z"  # type: ignore[arg-type,index]
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "PENDING_APPROVAL"
    assert body["impact_analysis"] is None
    assert body["impact_analysis_status"] == "unavailable"

    # approvals 中标识 unavailable
    approvals = await client.get("/api/v1/approvals", headers=ctx["leader_headers"])  # type: ignore[arg-type]
    entry = next(a for a in approvals.json() if a["id"] == body["id"])
    assert entry["impact_analysis_status"] == "unavailable"

    approved = await client.post(
        f"/api/v1/deadline-change-requests/{body['id']}/approve",
        json={"version": 1},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"
    assert approved.json()["impact_analysis_status"] == "unavailable"

    after = await _work_item(client, alice_headers, item["id"])  # type: ignore[arg-type,index]
    assert after["due_at"].startswith("2026-08-15")


# ---------- 权限与参数校验 ----------


async def test_create_permissions(client: httpx.AsyncClient, project: Project) -> None:
    """协作级仅协作双方可发起；主任务级仅主执行人或负责人；目标校验。"""
    ctx = await _setup(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    item = ctx["item"]
    collab = ctx["collab"]

    # 协作级：负责人不是协作双方 → 403
    resp = await _create_change(
        client, ctx["leader_headers"], item["id"], "collaboration_request", collab["id"], "2026-07-31T00:00:00Z"  # type: ignore[arg-type,index]
    )
    assert resp.status_code == 403

    # 主任务级：bob 既非主执行人也非负责人 → 403
    resp = await _create_change(
        client, ctx["bob_headers"], item["id"], "work_item", item["id"], "2026-08-15T00:00:00Z"  # type: ignore[arg-type,index]
    )
    assert resp.status_code == 403

    # 主任务级 target_id 必须是路径中的工作项 → 422
    resp = await _create_change(
        client, ctx["alice_headers"], item["id"], "work_item", str(uuid.uuid4()), "2026-08-15T00:00:00Z"  # type: ignore[arg-type,index]
    )
    assert resp.status_code == 422

    # 协作目标不存在 → 404
    resp = await _create_change(
        client, ctx["alice_headers"], item["id"], "collaboration_request", str(uuid.uuid4()), "2026-07-31T00:00:00Z"  # type: ignore[arg-type,index]
    )
    assert resp.status_code == 404

    # 协作双方均可发起（接收人 bob 发起，且新 DDL ≤ 主 DDL → 自动生效）
    resp = await _create_change(
        client, ctx["bob_headers"], item["id"], "collaboration_request", collab["id"], "2026-07-31T00:00:00Z"  # type: ignore[arg-type,index]
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "APPROVED"


async def test_terminal_collab_cannot_change_due(
    client: httpx.AsyncClient, project: Project
) -> None:
    """已结束的协作请求不允许变更 DDL → 422。"""
    ctx = await _setup(client, project)
    item = ctx["item"]
    collab = ctx["collab"]
    alice_headers = ctx["alice_headers"]

    cancelled = await client.post(
        f"/api/v1/collaboration-requests/{collab['id']}/cancel",  # type: ignore[index]
        json={"version": 1},
        headers=alice_headers,  # type: ignore[arg-type]
    )
    assert cancelled.status_code == 200

    resp = await _create_change(
        client, alice_headers, item["id"], "collaboration_request", collab["id"], "2026-07-31T00:00:00Z"  # type: ignore[arg-type,index]
    )
    assert resp.status_code == 422


async def test_approve_requires_leader(client: httpx.AsyncClient, project: Project) -> None:
    """非负责人审批主 DDL 变更 → 403；取消仅发起人。"""
    ctx = await _setup(client, project)
    item = ctx["item"]
    alice_headers = ctx["alice_headers"]

    created = await _create_change(
        client, alice_headers, item["id"], "work_item", item["id"], "2026-08-15T00:00:00Z"  # type: ignore[arg-type,index]
    )
    body = created.json()

    forbidden = await client.post(
        f"/api/v1/deadline-change-requests/{body['id']}/approve",
        json={"version": 1},
        headers=ctx["bob_headers"],  # type: ignore[arg-type]
    )
    assert forbidden.status_code == 403

    forbidden_cancel = await client.post(
        f"/api/v1/deadline-change-requests/{body['id']}/cancel",
        json={"version": 1},
        headers=ctx["bob_headers"],  # type: ignore[arg-type]
    )
    assert forbidden_cancel.status_code == 403

    cancelled = await client.post(
        f"/api/v1/deadline-change-requests/{body['id']}/cancel",
        json={"version": 1},
        headers=alice_headers,  # type: ignore[arg-type]
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    assert await _audit_actions(body["id"]) == [
        "deadline_change.requested",
        "deadline_change.cancelled",
    ]

    # 取消后主任务 DDL 不变
    after = await _work_item(client, alice_headers, item["id"])  # type: ignore[arg-type,index]
    assert after["due_at"].startswith("2026-08-01")


# ---------- 查询（12.4 节） ----------


async def test_list_endpoints(client: httpx.AsyncClient, project: Project) -> None:
    """工作项 DDL 变更历史（成员可查）；role=mine 我发起的。"""
    ctx = await _setup(client, project)
    item = ctx["item"]
    collab = ctx["collab"]
    alice_headers = ctx["alice_headers"]

    created = await _create_change(
        client, alice_headers, item["id"], "collaboration_request", collab["id"], "2026-07-31T00:00:00Z"  # type: ignore[arg-type,index]
    )
    change_id = created.json()["id"]

    by_item = await client.get(
        f"/api/v1/work-items/{item['id']}/deadline-change-requests",  # type: ignore[index]
        headers=ctx["bob_headers"],  # type: ignore[arg-type]
    )
    assert by_item.status_code == 200
    assert [r["id"] for r in by_item.json()] == [change_id]
    summary = by_item.json()[0]
    assert summary["target_title"] == "标注训练样本"
    assert summary["work_item_title"] == "RAG 工作项"
    assert "impact_analysis" not in summary  # 摘要不含分析正文

    mine = await client.get("/api/v1/deadline-change-requests?role=mine", headers=alice_headers)  # type: ignore[arg-type]
    assert [r["id"] for r in mine.json()] == [change_id]
    mine_bob = await client.get(
        "/api/v1/deadline-change-requests?role=mine", headers=ctx["bob_headers"]  # type: ignore[arg-type]
    )
    assert mine_bob.json() == []

    missing = await client.get(
        f"/api/v1/work-items/{uuid.uuid4()}/deadline-change-requests",
        headers=alice_headers,  # type: ignore[arg-type]
    )
    assert missing.status_code == 404


async def test_get_detail(client: httpx.AsyncClient, project: Project) -> None:
    """单条详情：成员可查，含 reason 与 impact_analysis 正文；不存在 → 404 统一格式。"""
    ctx = await _setup(client, project)
    item = ctx["item"]
    collab = ctx["collab"]
    alice_headers = ctx["alice_headers"]

    created = await _create_change(
        client, alice_headers, item["id"], "collaboration_request", collab["id"], "2026-07-31T00:00:00Z"  # type: ignore[arg-type,index]
    )
    change_id = created.json()["id"]

    detail = await client.get(
        f"/api/v1/deadline-change-requests/{change_id}",
        headers=ctx["bob_headers"],  # type: ignore[arg-type] 成员即可查
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["id"] == change_id
    assert body["reason"] == "依赖方延期，需要顺延"
    assert body["impact_analysis_status"] == "generated"
    analysis = body["impact_analysis"]
    assert analysis["exceeds_work_item_due"] is False
    assert analysis["target"]["id"] == collab["id"]  # type: ignore[index]
    assert analysis["work_item"]["due_at"].startswith("2026-08-01")
    assert collab["id"] in {  # type: ignore[index]
        c["id"] for c in analysis["affected_collaboration_requests"]
    }

    missing = await client.get(
        f"/api/v1/deadline-change-requests/{uuid.uuid4()}",
        headers=alice_headers,  # type: ignore[arg-type]
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "NOT_FOUND"
