"""协作请求 API 与站内通知集成测试。"""

import uuid

import httpx
from sqlalchemy import func, select

from app.domains.audit.models import AuditEvent
from app.domains.notifications.models import Notification
from app.domains.project.models import Project, ProjectMember
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member, auth_headers

LEADER_PW = "Leader123!"
ALICE_PW = "Alice123!"
BOB_PW = "Bob123!"


async def _setup(client: httpx.AsyncClient, project: Project) -> dict[str, object]:
    """返回负责人、工作项主执行人、协作接收人及已发布工作项。"""
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
            "priority": "high",
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


async def _create_request(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    item_id: str,
    assignee_id: str,
    *,
    idempotency_key: str | None = None,
    **extra: object,
) -> httpx.Response:
    payload = {
        "assignee_id": assignee_id,
        "title": "标注训练样本",
        "goal": "完成 100 条样本标注",
        "template": "样本ID,标签",
        "due_at": "2026-07-30T00:00:00Z",
        **extra,
    }
    req_headers = dict(headers)
    if idempotency_key:
        req_headers["Idempotency-Key"] = idempotency_key
    return await client.post(
        f"/api/v1/work-items/{item_id}/collaboration-requests",
        json=payload,
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


async def _count_notifications() -> int:
    async with async_session_factory() as session:
        return (
            await session.execute(select(func.count()).select_from(Notification))
        ).scalar_one()


async def test_full_chain_and_work_item_untouched(
    client: httpx.AsyncClient, project: Project
) -> None:
    """发起 → 接受 → 开始 → 提交 → 完成；每步有审计与通知；工作项主执行人/状态不受影响。"""
    ctx = await _setup(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    item = ctx["item"]
    alice_headers = ctx["alice_headers"]
    bob_headers = ctx["bob_headers"]

    created = await _create_request(client, alice_headers, item["id"], str(bob.id))
    assert created.status_code == 201, created.text
    req = created.json()
    assert req["status"] == "REQUESTED"
    assert req["version"] == 1
    assert req["work_item_id"] == item["id"]
    assert req["work_item_title"] == "RAG 工作项"
    assert req["requester"] == {"id": str(alice.id), "display_name": "爱丽丝"}
    assert req["assignee"] == {"id": str(bob.id), "display_name": "鲍勃"}
    assert req["result_text"] is None
    req_id = req["id"]

    accepted = await client.post(
        f"/api/v1/collaboration-requests/{req_id}/accept",
        json={"version": 1},
        headers=bob_headers,  # type: ignore[arg-type]
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "ACCEPTED"

    started = await client.post(
        f"/api/v1/collaboration-requests/{req_id}/start",
        json={"version": 2},
        headers=bob_headers,  # type: ignore[arg-type]
    )
    assert started.json()["status"] == "IN_PROGRESS"

    submitted = await client.post(
        f"/api/v1/collaboration-requests/{req_id}/submit",
        json={"version": 3, "result_text": "已标注 100 条样本，见附件文本"},
        headers=bob_headers,  # type: ignore[arg-type]
    )
    assert submitted.json()["status"] == "SUBMITTED"
    assert submitted.json()["result_text"] == "已标注 100 条样本，见附件文本"

    completed = await client.post(
        f"/api/v1/collaboration-requests/{req_id}/complete",
        json={"version": 4},
        headers=alice_headers,  # type: ignore[arg-type]
    )
    assert completed.json()["status"] == "COMPLETED"
    assert completed.json()["version"] == 5

    # 每次迁移均有审计事件
    assert await _audit_actions(req_id) == [
        "collaboration.requested",
        "collaboration.accepted",
        "collaboration.started",
        "collaboration.submitted",
        "collaboration.completed",
    ]

    bob_types = [n.type for n in await _notifications_of(bob.id)]
    alice_types = [n.type for n in await _notifications_of(alice.id)]
    assert bob_types == ["collaboration.requested", "collaboration.completed"]
    assert alice_types == ["collaboration.accepted", "collaboration.submitted"]

    work_item = (
        await client.get(f"/api/v1/work-items/{item['id']}", headers=alice_headers)  # type: ignore[arg-type]
    ).json()
    assert work_item["assignee"]["id"] == str(alice.id)
    assert work_item["status"] == "READY"
    # 接收人已加入协作者列表
    assert {c["id"] for c in work_item["collaborators"]} == {str(bob.id)}


async def test_decline_branch(client: httpx.AsyncClient, project: Project) -> None:
    """接收人拒绝 → DECLINED；终态后 accept 非法；发起人收到拒绝通知。"""
    ctx = await _setup(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    created = await _create_request(
        client, ctx["alice_headers"], ctx["item"]["id"], str(bob.id)  # type: ignore[arg-type,index]
    )
    req_id = created.json()["id"]

    declined = await client.post(
        f"/api/v1/collaboration-requests/{req_id}/decline",
        json={"version": 1},
        headers=ctx["bob_headers"],  # type: ignore[arg-type]
    )
    assert declined.status_code == 200
    assert declined.json()["status"] == "DECLINED"

    retry = await client.post(
        f"/api/v1/collaboration-requests/{req_id}/accept",
        json={"version": 2},
        headers=ctx["bob_headers"],  # type: ignore[arg-type]
    )
    assert retry.status_code == 409
    assert retry.json()["code"] == "COLLABORATION_INVALID_TRANSITION"

    alice_types = [n.type for n in await _notifications_of(alice.id)]
    assert alice_types == ["collaboration.declined"]


async def test_revision_branch(client: httpx.AsyncClient, project: Project) -> None:
    """提交 → 发起人要求修改（反馈入审计）→ 继续处理 → 再提交 → 完成。"""
    ctx = await _setup(client, project)
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    alice_headers = ctx["alice_headers"]
    bob_headers = ctx["bob_headers"]
    created = await _create_request(
        client, alice_headers, ctx["item"]["id"], str(bob.id)  # type: ignore[arg-type,index]
    )
    req_id = created.json()["id"]
    base = f"/api/v1/collaboration-requests/{req_id}"

    await client.post(f"{base}/accept", json={"version": 1}, headers=bob_headers)  # type: ignore[arg-type]
    await client.post(f"{base}/start", json={"version": 2}, headers=bob_headers)  # type: ignore[arg-type]
    await client.post(
        f"{base}/submit",
        json={"version": 3, "result_text": "第一版标注"},
        headers=bob_headers,  # type: ignore[arg-type]
    )

    revision = await client.post(
        f"{base}/request-revision",
        json={"version": 4, "feedback": "第 12、37 条标签有误，请修正"},
        headers=alice_headers,  # type: ignore[arg-type]
    )
    assert revision.status_code == 200
    assert revision.json()["status"] == "REVISION_REQUESTED"

    async with async_session_factory() as session:
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.target_id == uuid.UUID(req_id),
                    AuditEvent.action == "collaboration.revision_requested",
                )
            )
        ).scalar_one()
    assert event.after["feedback"] == "第 12、37 条标签有误，请修正"

    bob_notifications = await _notifications_of(bob.id)
    revision_notice = [n for n in bob_notifications if n.type == "collaboration.revision_requested"]
    assert len(revision_notice) == 1
    assert "第 12、37 条" not in revision_notice[0].body  # 通知正文不含反馈细节

    # 继续处理复用 start，然后重新提交并完成
    restarted = await client.post(f"{base}/start", json={"version": 5}, headers=bob_headers)  # type: ignore[arg-type]
    assert restarted.json()["status"] == "IN_PROGRESS"
    await client.post(
        f"{base}/submit",
        json={"version": 6, "result_text": "第二版标注（已修正）"},
        headers=bob_headers,  # type: ignore[arg-type]
    )
    completed = await client.post(f"{base}/complete", json={"version": 7}, headers=alice_headers)  # type: ignore[arg-type]
    assert completed.json()["status"] == "COMPLETED"
    assert completed.json()["result_text"] == "第二版标注（已修正）"


async def test_cancel_by_requester_and_assignee(
    client: httpx.AsyncClient, project: Project
) -> None:
    """REQUESTED 可由发起人取消，ACCEPTED 可由接收人取消。"""
    ctx = await _setup(client, project)
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    alice_headers = ctx["alice_headers"]
    bob_headers = ctx["bob_headers"]
    item_id = ctx["item"]["id"]  # type: ignore[index]

    first = (await _create_request(client, alice_headers, item_id, str(bob.id))).json()  # type: ignore[arg-type]
    cancelled = await client.post(
        f"/api/v1/collaboration-requests/{first['id']}/cancel",
        json={"version": 1},
        headers=alice_headers,  # type: ignore[arg-type]
    )
    assert cancelled.json()["status"] == "CANCELLED"

    second = (await _create_request(client, alice_headers, item_id, str(bob.id))).json()  # type: ignore[arg-type]
    await client.post(
        f"/api/v1/collaboration-requests/{second['id']}/accept",
        json={"version": 1},
        headers=bob_headers,  # type: ignore[arg-type]
    )
    cancelled2 = await client.post(
        f"/api/v1/collaboration-requests/{second['id']}/cancel",
        json={"version": 2},
        headers=bob_headers,  # type: ignore[arg-type]
    )
    assert cancelled2.json()["status"] == "CANCELLED"

    third = (await _create_request(client, alice_headers, item_id, str(bob.id))).json()  # type: ignore[arg-type]
    base = f"/api/v1/collaboration-requests/{third['id']}"
    await client.post(f"{base}/accept", json={"version": 1}, headers=bob_headers)  # type: ignore[arg-type]
    await client.post(f"{base}/start", json={"version": 2}, headers=bob_headers)  # type: ignore[arg-type]
    bad_cancel = await client.post(f"{base}/cancel", json={"version": 3}, headers=alice_headers)  # type: ignore[arg-type]
    assert bad_cancel.status_code == 409


async def test_non_assignee_cannot_accept(client: httpx.AsyncClient, project: Project) -> None:
    """非接收人调用 accept → 403。"""
    ctx = await _setup(client, project)
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    created = await _create_request(
        client, ctx["alice_headers"], ctx["item"]["id"], str(bob.id)  # type: ignore[arg-type,index]
    )
    req_id = created.json()["id"]

    resp = await client.post(
        f"/api/v1/collaboration-requests/{req_id}/accept",
        json={"version": 1},
        headers=ctx["alice_headers"],  # type: ignore[arg-type]
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"


async def test_only_work_item_assignee_can_create(
    client: httpx.AsyncClient, project: Project
) -> None:
    """非工作项当前主执行人发起协作 → 403；发给自己 → 422；接收人非活跃成员 → 422。"""
    ctx = await _setup(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    leader: ProjectMember = ctx["leader"]  # type: ignore[assignment]
    item_id = ctx["item"]["id"]  # type: ignore[index]

    # bob 不是主执行人，不能发起
    resp = await _create_request(client, ctx["bob_headers"], item_id, str(alice.id))  # type: ignore[arg-type]
    assert resp.status_code == 403

    resp = await _create_request(client, ctx["leader_headers"], item_id, str(bob.id))  # type: ignore[arg-type]
    assert resp.status_code == 403

    # 不能发给自己
    resp = await _create_request(client, ctx["alice_headers"], item_id, str(alice.id))  # type: ignore[arg-type]
    assert resp.status_code == 422

    # 不存在的成员
    resp = await _create_request(client, ctx["alice_headers"], item_id, str(uuid.uuid4()))  # type: ignore[arg-type]
    assert resp.status_code == 422

    # 被禁用的成员
    await client.patch(  # leader 禁用 bob
        f"/api/v1/members/{bob.id}",
        json={"is_active": False},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    resp = await _create_request(client, ctx["alice_headers"], item_id, str(bob.id))  # type: ignore[arg-type]
    assert resp.status_code == 422


async def test_command_role_checks(client: httpx.AsyncClient, project: Project) -> None:
    """request-revision / complete 仅发起人；submit 仅接收人。"""
    ctx = await _setup(client, project)
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    alice_headers = ctx["alice_headers"]
    bob_headers = ctx["bob_headers"]
    created = await _create_request(client, alice_headers, ctx["item"]["id"], str(bob.id))  # type: ignore[arg-type,index]
    req_id = created.json()["id"]
    base = f"/api/v1/collaboration-requests/{req_id}"

    await client.post(f"{base}/accept", json={"version": 1}, headers=bob_headers)  # type: ignore[arg-type]
    await client.post(f"{base}/start", json={"version": 2}, headers=bob_headers)  # type: ignore[arg-type]

    # 发起人不能 submit
    resp = await client.post(
        f"{base}/submit", json={"version": 3, "result_text": "x"}, headers=alice_headers  # type: ignore[arg-type]
    )
    assert resp.status_code == 403

    await client.post(
        f"{base}/submit", json={"version": 3, "result_text": "x"}, headers=bob_headers  # type: ignore[arg-type]
    )

    # 接收人不能 request-revision / complete
    resp = await client.post(f"{base}/request-revision", json={"version": 4}, headers=bob_headers)  # type: ignore[arg-type]
    assert resp.status_code == 403
    resp = await client.post(f"{base}/complete", json={"version": 4}, headers=bob_headers)  # type: ignore[arg-type]
    assert resp.status_code == 403


async def test_idempotent_create_no_duplicate_side_effects(
    client: httpx.AsyncClient, project: Project
) -> None:
    """同一幂等键重复发起：不产生重复请求、重复审计事件与重复通知。"""
    ctx = await _setup(client, project)
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    key = str(uuid.uuid4())
    first = await _create_request(
        client, ctx["alice_headers"], ctx["item"]["id"], str(bob.id), idempotency_key=key  # type: ignore[arg-type,index]
    )
    assert first.status_code == 201
    replay = await _create_request(
        client, ctx["alice_headers"], ctx["item"]["id"], str(bob.id), idempotency_key=key  # type: ignore[arg-type,index]
    )
    assert replay.status_code == 201
    assert replay.headers.get("Idempotency-Replayed") == "true"
    assert replay.json()["id"] == first.json()["id"]

    assert await _audit_actions(first.json()["id"]) == ["collaboration.requested"]
    assert await _count_notifications() == 1

    req_id = first.json()["id"]
    accept_key = str(uuid.uuid4())
    headers = {**ctx["bob_headers"], "Idempotency-Key": accept_key}  # type: ignore[dict-item]
    r1 = await client.post(
        f"/api/v1/collaboration-requests/{req_id}/accept", json={"version": 1}, headers=headers
    )
    r2 = await client.post(
        f"/api/v1/collaboration-requests/{req_id}/accept", json={"version": 1}, headers=headers
    )
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.headers.get("Idempotency-Replayed") == "true"
    assert await _audit_actions(req_id) == ["collaboration.requested", "collaboration.accepted"]
    assert await _count_notifications() == 2  # requested + accepted 各一条


async def test_version_conflict(client: httpx.AsyncClient, project: Project) -> None:
    """过期 version → 409 COLLABORATION_VERSION_CONFLICT，details 带当前版本。"""
    ctx = await _setup(client, project)
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    created = await _create_request(
        client, ctx["alice_headers"], ctx["item"]["id"], str(bob.id)  # type: ignore[arg-type,index]
    )
    req_id = created.json()["id"]
    await client.post(
        f"/api/v1/collaboration-requests/{req_id}/accept",
        json={"version": 1},
        headers=ctx["bob_headers"],  # type: ignore[arg-type]
    )
    stale = await client.post(
        f"/api/v1/collaboration-requests/{req_id}/start",
        json={"version": 1},  # 已推进到 version=2
        headers=ctx["bob_headers"],  # type: ignore[arg-type]
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "COLLABORATION_VERSION_CONFLICT"
    assert stale.json()["details"]["current_version"] == 2


async def test_list_endpoints(client: httpx.AsyncClient, project: Project) -> None:
    """工作项协作列表（成员可查）；我发出的/我收到的摘要列表。"""
    ctx = await _setup(client, project)
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    item_id = ctx["item"]["id"]  # type: ignore[index]
    created = await _create_request(client, ctx["alice_headers"], item_id, str(bob.id))  # type: ignore[arg-type]

    by_item = await client.get(
        f"/api/v1/work-items/{item_id}/collaboration-requests",
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert by_item.status_code == 200
    assert [r["id"] for r in by_item.json()] == [created.json()["id"]]
    summary = by_item.json()[0]
    assert summary["work_item_title"] == "RAG 工作项"
    assert summary["status"] == "REQUESTED"

    sent = await client.get(
        "/api/v1/collaboration-requests?role=sent", headers=ctx["alice_headers"]  # type: ignore[arg-type]
    )
    assert [r["id"] for r in sent.json()] == [created.json()["id"]]
    received = await client.get(
        "/api/v1/collaboration-requests?role=received", headers=ctx["alice_headers"]  # type: ignore[arg-type]
    )
    assert received.json() == []
    received_bob = await client.get(
        "/api/v1/collaboration-requests?role=received", headers=ctx["bob_headers"]  # type: ignore[arg-type]
    )
    assert [r["id"] for r in received_bob.json()] == [created.json()["id"]]

    missing = await client.get(
        f"/api/v1/work-items/{uuid.uuid4()}/collaboration-requests",
        headers=ctx["alice_headers"],  # type: ignore[arg-type]
    )
    assert missing.status_code == 404


async def test_get_detail(client: httpx.AsyncClient, project: Project) -> None:
    """单条详情：成员可查，含 goal/template/result_text 正文；不存在 → 404 统一格式。"""
    ctx = await _setup(client, project)
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    created = await _create_request(
        client, ctx["alice_headers"], ctx["item"]["id"], str(bob.id)  # type: ignore[arg-type,index]
    )
    req_id = created.json()["id"]

    detail = await client.get(
        f"/api/v1/collaboration-requests/{req_id}",
        headers=ctx["leader_headers"],  # type: ignore[arg-type] 成员即可查
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["id"] == req_id
    assert body["goal"] == "完成 100 条样本标注"
    assert body["template"] == "样本ID,标签"
    assert body["result_text"] is None
    assert body["work_item_title"] == "RAG 工作项"

    missing = await client.get(
        f"/api/v1/collaboration-requests/{uuid.uuid4()}",
        headers=ctx["alice_headers"],  # type: ignore[arg-type]
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "NOT_FOUND"


async def test_notifications_query_and_read_idempotent(
    client: httpx.AsyncClient, project: Project
) -> None:
    """仅本人可查/已读；已读幂等；unread_only 过滤与未读计数正确。"""
    ctx = await _setup(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    alice_headers = ctx["alice_headers"]
    bob_headers = ctx["bob_headers"]
    await _create_request(client, alice_headers, ctx["item"]["id"], str(bob.id))  # type: ignore[arg-type,index]

    listed = await client.get("/api/v1/notifications", headers=bob_headers)  # type: ignore[arg-type]
    assert listed.status_code == 200
    body = listed.json()
    assert body["unread_count"] == 1
    assert len(body["items"]) == 1
    notice = body["items"][0]
    assert notice["type"] == "collaboration.requested"
    assert notice["is_read"] is False
    assert notice["read_at"] is None
    assert notice["link"].startswith("/work-items/")

    forbidden = await client.post(
        f"/api/v1/notifications/{notice['id']}/read", headers=alice_headers  # type: ignore[arg-type]
    )
    assert forbidden.status_code == 404

    # 本人已读，且重复已读幂等
    for _ in range(2):
        read = await client.post(
            f"/api/v1/notifications/{notice['id']}/read", headers=bob_headers  # type: ignore[arg-type]
        )
        assert read.status_code == 200
    assert read.json()["is_read"] is True
    assert read.json()["read_at"] is not None

    after = (await client.get("/api/v1/notifications", headers=bob_headers)).json()  # type: ignore[arg-type]
    assert after["unread_count"] == 0
    unread_only = (
        await client.get("/api/v1/notifications?unread_only=true", headers=bob_headers)  # type: ignore[arg-type]
    ).json()
    assert unread_only["items"] == []

    missing = await client.post(
        f"/api/v1/notifications/{uuid.uuid4()}/read", headers=bob_headers  # type: ignore[arg-type]
    )
    assert missing.status_code == 404
