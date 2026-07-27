"""转派申请 API 集成测试（T3.3 验收，7.3、8.3、12.4、16、17.2 节）。"""

import uuid

import httpx
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
    """准备：leader（负责人）+ alice（工作项主执行人）+ bob（转派目标成员）。"""
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


async def _create_transfer(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    item_id: str,
    to_member_id: str,
    *,
    idempotency_key: str | None = None,
) -> httpx.Response:
    req_headers = dict(headers)
    if idempotency_key:
        req_headers["Idempotency-Key"] = idempotency_key
    return await client.post(
        f"/api/v1/work-items/{item_id}/transfer-requests",
        json={
            "to_member_id": to_member_id,
            "reason": "超出我的能力范围",
            "impact_note": "DDL 不变，进行中的协作请求由新负责人接管",
        },
        headers=req_headers,
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


# ---------- 审批通过链路（7.3 节） ----------


async def test_approve_chain_assignee_audit_notifications(
    client: httpx.AsyncClient, project: Project
) -> None:
    """发起 → 负责人在 approvals 看到 → 通过：同事务 assignee 更新 + 审计 + 双方通知。"""
    ctx = await _setup(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    leader: ProjectMember = ctx["leader"]  # type: ignore[assignment]
    item = ctx["item"]
    alice_headers = ctx["alice_headers"]
    leader_headers = ctx["leader_headers"]

    created = await _create_transfer(client, alice_headers, item["id"], str(bob.id))  # type: ignore[arg-type,index]
    assert created.status_code == 201, created.text
    req = created.json()
    assert req["status"] == "PENDING"
    assert req["version"] == 1
    assert req["from_member"] == {"id": str(alice.id), "display_name": "爱丽丝"}
    assert req["to_member"] == {"id": str(bob.id), "display_name": "鲍勃"}
    assert req["agent_suggestion_id"] is None
    req_id = req["id"]

    # 审批前主任务负责人不变化（7.3 节）
    before = await _work_item(client, alice_headers, item["id"])  # type: ignore[arg-type,index]
    assert before["assignee"]["id"] == str(alice.id)

    # 负责人收到待审批通知，GET /approvals 可见
    leader_types = [n.type for n in await _notifications_of(leader.id)]
    assert "transfer.requested" in leader_types
    approvals = await client.get("/api/v1/approvals", headers=leader_headers)  # type: ignore[arg-type]
    assert [a["id"] for a in approvals.json()] == [req_id]
    assert approvals.json()[0]["kind"] == "transfer"

    # 负责人通过（带审批意见）
    approved = await client.post(
        f"/api/v1/transfer-requests/{req_id}/approve",
        json={"version": 1, "decision_note": "同意，鲍勃更匹配"},
        headers=leader_headers,  # type: ignore[arg-type]
    )
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["status"] == "APPROVED"
    assert body["version"] == 2
    assert body["approved_by"] == {"id": str(leader.id), "display_name": "负责人"}
    assert body["approved_at"] is not None

    # 同事务：assignee 更新 + version+1（publish 后为 2 → 3）
    after = await _work_item(client, alice_headers, item["id"])  # type: ignore[arg-type,index]
    assert after["assignee"]["id"] == str(bob.id)
    assert after["version"] == 3

    # 审计：申请侧 + 工作项侧负责人变更留痕（历史负责人可追溯）
    assert await _audit_actions(req_id) == ["transfer.requested", "transfer.approved"]
    async with async_session_factory() as session:
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.target_id == uuid.UUID(item["id"]),  # type: ignore[index]
                    AuditEvent.action == "work_item.assignee_changed",
                )
            )
        ).scalar_one()
    assert event.before == {"assignee_id": str(alice.id)}
    assert event.after["assignee_id"] == str(bob.id)
    assert event.after["transfer_request_id"] == req_id

    # 审批意见入审计，不进通知正文（16 节）
    async with async_session_factory() as session:
        approved_event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.target_id == uuid.UUID(req_id),
                    AuditEvent.action == "transfer.approved",
                )
            )
        ).scalar_one()
    assert approved_event.after["decision_note"] == "同意，鲍勃更匹配"

    # 新旧负责人均收到通知
    alice_types = [n.type for n in await _notifications_of(alice.id)]
    bob_notifications = await _notifications_of(bob.id)
    assert "transfer.approved" in alice_types
    assert [n.type for n in bob_notifications] == ["transfer.approved"]
    assert "同意" not in bob_notifications[0].body

    # 审批后 approvals 列表清空
    approvals = await client.get("/api/v1/approvals", headers=leader_headers)  # type: ignore[arg-type]
    assert approvals.json() == []


# ---------- 唯一待审批约束（8.3、17.2 节） ----------


async def test_pending_conflict(client: httpx.AsyncClient, project: Project) -> None:
    """存在 PENDING 转派时同工作项再发起 → 409 TRANSFER_PENDING_CONFLICT。"""
    ctx = await _setup(client, project)
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    leader: ProjectMember = ctx["leader"]  # type: ignore[assignment]
    alice_headers = ctx["alice_headers"]
    item_id = ctx["item"]["id"]  # type: ignore[index]

    first = await _create_transfer(client, alice_headers, item_id, str(bob.id))  # type: ignore[arg-type]
    assert first.status_code == 201

    second = await _create_transfer(client, alice_headers, item_id, str(leader.id))  # type: ignore[arg-type]
    assert second.status_code == 409
    assert second.json()["code"] == "TRANSFER_PENDING_CONFLICT"
    assert second.json()["details"]["pending_request_id"] == first.json()["id"]

    # 取消后可再次发起
    cancelled = await client.post(
        f"/api/v1/transfer-requests/{first.json()['id']}/cancel",
        json={"version": 1},
        headers=alice_headers,  # type: ignore[arg-type]
    )
    assert cancelled.status_code == 200
    third = await _create_transfer(client, alice_headers, item_id, str(leader.id))  # type: ignore[arg-type]
    assert third.status_code == 201


# ---------- 并发/重复审批只生效一次（17.2 节） ----------


async def test_repeat_approve_only_effective_once(
    client: httpx.AsyncClient, project: Project
) -> None:
    """同幂等键重放返回首次结果；不同键重复 approve → 409，副作用只发生一次。"""
    ctx = await _setup(client, project)
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    item = ctx["item"]
    alice_headers = ctx["alice_headers"]
    leader_headers = ctx["leader_headers"]

    req_id = (
        await _create_transfer(client, alice_headers, item["id"], str(bob.id))  # type: ignore[arg-type,index]
    ).json()["id"]

    key = str(uuid.uuid4())
    headers = {**leader_headers, "Idempotency-Key": key}  # type: ignore[dict-item]
    r1 = await client.post(
        f"/api/v1/transfer-requests/{req_id}/approve", json={"version": 1}, headers=headers
    )
    r2 = await client.post(
        f"/api/v1/transfer-requests/{req_id}/approve", json={"version": 1}, headers=headers
    )
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.headers.get("Idempotency-Replayed") == "true"
    assert r2.json()["id"] == r1.json()["id"]

    # 不同幂等键重复审批：版本已推进 → 409；再次尝试 → 状态机 409
    again = await client.post(
        f"/api/v1/transfer-requests/{req_id}/approve",
        json={"version": 1},
        headers=leader_headers,  # type: ignore[arg-type]
    )
    assert again.status_code == 409
    assert again.json()["code"] == "TRANSFER_VERSION_CONFLICT"
    again2 = await client.post(
        f"/api/v1/transfer-requests/{req_id}/approve",
        json={"version": 2},
        headers=leader_headers,  # type: ignore[arg-type]
    )
    assert again2.status_code == 409
    assert again2.json()["code"] == "TRANSFER_INVALID_TRANSITION"

    # 副作用只发生一次：一条 approved 审计、一条 assignee 变更审计、双方各一条通知
    assert await _audit_actions(req_id) == ["transfer.requested", "transfer.approved"]
    async with async_session_factory() as session:
        assignee_events = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.target_id == uuid.UUID(item["id"]),  # type: ignore[index]
                        AuditEvent.action == "work_item.assignee_changed",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(assignee_events) == 1
    bob_types = [n.type for n in await _notifications_of(bob.id)]
    assert bob_types == ["transfer.approved"]


# ---------- 驳回与取消 ----------


async def test_reject_keeps_assignee(client: httpx.AsyncClient, project: Project) -> None:
    """负责人驳回：assignee 不变，意见入审计不进通知，发起人收到驳回通知。"""
    ctx = await _setup(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    item = ctx["item"]
    alice_headers = ctx["alice_headers"]

    req_id = (
        await _create_transfer(client, alice_headers, item["id"], str(bob.id))  # type: ignore[arg-type,index]
    ).json()["id"]
    rejected = await client.post(
        f"/api/v1/transfer-requests/{req_id}/reject",
        json={"version": 1, "decision_note": "当前阶段不宜更换负责人"},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "REJECTED"

    after = await _work_item(client, alice_headers, item["id"])  # type: ignore[arg-type,index]
    assert after["assignee"]["id"] == str(alice.id)
    assert after["version"] == 2  # 仅 publish 推进过一次

    assert await _audit_actions(req_id) == ["transfer.requested", "transfer.rejected"]
    alice_notifications = await _notifications_of(alice.id)
    assert [n.type for n in alice_notifications] == ["transfer.rejected"]
    assert "不宜更换" not in alice_notifications[0].body
    # 驳回后可再次发起
    again = await _create_transfer(client, alice_headers, item["id"], str(bob.id))  # type: ignore[arg-type,index]
    assert again.status_code == 201


async def test_cancel_only_by_requester(client: httpx.AsyncClient, project: Project) -> None:
    """发起人可取消自己的 PENDING；他人取消 → 403；取消产生审计。"""
    ctx = await _setup(client, project)
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    alice_headers = ctx["alice_headers"]
    item_id = ctx["item"]["id"]  # type: ignore[index]

    req_id = (await _create_transfer(client, alice_headers, item_id, str(bob.id))).json()["id"]

    forbidden = await client.post(
        f"/api/v1/transfer-requests/{req_id}/cancel",
        json={"version": 1},
        headers=ctx["bob_headers"],  # type: ignore[arg-type]
    )
    assert forbidden.status_code == 403

    cancelled = await client.post(
        f"/api/v1/transfer-requests/{req_id}/cancel",
        json={"version": 1},
        headers=alice_headers,  # type: ignore[arg-type]
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    assert await _audit_actions(req_id) == ["transfer.requested", "transfer.cancelled"]


# ---------- 权限与参数校验 ----------


async def test_create_permissions(client: httpx.AsyncClient, project: Project) -> None:
    """仅当前主执行人可发起；不能转给自己；目标必须是活跃成员。"""
    ctx = await _setup(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    item_id = ctx["item"]["id"]  # type: ignore[index]

    # bob 不是主执行人
    resp = await _create_transfer(client, ctx["bob_headers"], item_id, str(alice.id))  # type: ignore[arg-type]
    assert resp.status_code == 403
    # 负责人但不是主执行人
    resp = await _create_transfer(client, ctx["leader_headers"], item_id, str(bob.id))  # type: ignore[arg-type]
    assert resp.status_code == 403
    # 不能转给自己
    resp = await _create_transfer(client, ctx["alice_headers"], item_id, str(alice.id))  # type: ignore[arg-type]
    assert resp.status_code == 422
    # 目标成员不存在
    resp = await _create_transfer(client, ctx["alice_headers"], item_id, str(uuid.uuid4()))  # type: ignore[arg-type]
    assert resp.status_code == 422


async def test_approve_requires_leader_and_version(
    client: httpx.AsyncClient, project: Project
) -> None:
    """非负责人审批 → 403；过期 version → 409 带当前版本。"""
    ctx = await _setup(client, project)
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    req_id = (
        await _create_transfer(client, ctx["alice_headers"], ctx["item"]["id"], str(bob.id))  # type: ignore[arg-type,index]
    ).json()["id"]

    forbidden = await client.post(
        f"/api/v1/transfer-requests/{req_id}/approve",
        json={"version": 1},
        headers=ctx["alice_headers"],  # type: ignore[arg-type]
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "FORBIDDEN"

    # 先取消推进 version，再用旧 version 审批
    await client.post(
        f"/api/v1/transfer-requests/{req_id}/cancel",
        json={"version": 1},
        headers=ctx["alice_headers"],  # type: ignore[arg-type]
    )
    stale = await client.post(
        f"/api/v1/transfer-requests/{req_id}/approve",
        json={"version": 1},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "TRANSFER_VERSION_CONFLICT"
    assert stale.json()["details"]["current_version"] == 2


# ---------- 查询（12.4 节） ----------


async def test_list_endpoints(client: httpx.AsyncClient, project: Project) -> None:
    """工作项转派历史（成员可查）；role=mine 我发起的。"""
    ctx = await _setup(client, project)
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    item_id = ctx["item"]["id"]  # type: ignore[index]
    created = await _create_transfer(client, ctx["alice_headers"], item_id, str(bob.id))  # type: ignore[arg-type]
    req_id = created.json()["id"]

    by_item = await client.get(
        f"/api/v1/work-items/{item_id}/transfer-requests",
        headers=ctx["bob_headers"],  # type: ignore[arg-type]
    )
    assert by_item.status_code == 200
    assert [r["id"] for r in by_item.json()] == [req_id]
    assert by_item.json()[0]["work_item_title"] == "RAG 工作项"
    assert "reason" not in by_item.json()[0]  # 摘要不含正文

    mine = await client.get("/api/v1/transfer-requests?role=mine", headers=ctx["alice_headers"])  # type: ignore[arg-type]
    assert [r["id"] for r in mine.json()] == [req_id]
    mine_bob = await client.get("/api/v1/transfer-requests?role=mine", headers=ctx["bob_headers"])  # type: ignore[arg-type]
    assert mine_bob.json() == []

    missing = await client.get(
        f"/api/v1/work-items/{uuid.uuid4()}/transfer-requests",
        headers=ctx["alice_headers"],  # type: ignore[arg-type]
    )
    assert missing.status_code == 404


async def test_get_detail(client: httpx.AsyncClient, project: Project) -> None:
    """单条详情：成员可查，含 reason/impact_note 正文；不存在 → 404 统一格式。"""
    ctx = await _setup(client, project)
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    created = await _create_transfer(
        client, ctx["alice_headers"], ctx["item"]["id"], str(bob.id)  # type: ignore[arg-type,index]
    )
    req_id = created.json()["id"]

    detail = await client.get(
        f"/api/v1/transfer-requests/{req_id}",
        headers=ctx["bob_headers"],  # type: ignore[arg-type] 成员即可查
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["id"] == req_id
    assert body["reason"] == "超出我的能力范围"
    assert body["impact_note"] == "DDL 不变，进行中的协作请求由新负责人接管"
    assert body["status"] == "PENDING"

    missing = await client.get(
        f"/api/v1/transfer-requests/{uuid.uuid4()}",
        headers=ctx["alice_headers"],  # type: ignore[arg-type]
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "NOT_FOUND"
