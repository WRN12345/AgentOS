"""工作项命令 API 集成测试（T2.6 验收，6.1、7.1、12.3、17.2 节）。"""

import httpx
from sqlalchemy import select

from app.domains.audit.models import AuditEvent
from app.domains.project.models import Project, ProjectMember
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member, auth_headers

LEADER_PW = "Leader123!"
ALICE_PW = "Alice123!"
BOB_PW = "Bob123!"


async def _setup(client: httpx.AsyncClient, project: Project) -> dict[str, object]:
    """准备：leader（负责人）+ alice / bob 两名普通成员，返回成员与请求头。"""
    _, leader = await add_member(project, "leader", LEADER_PW, role="leader", display_name="负责人")
    _, alice = await add_member(project, "alice", ALICE_PW, display_name="爱丽丝")
    _, bob = await add_member(project, "bob", BOB_PW, display_name="鲍勃")
    return {
        "leader": leader,
        "alice": alice,
        "bob": bob,
        "leader_headers": await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id)),
        "alice_headers": await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id)),
        "bob_headers": await auth_headers(client, "bob", BOB_PW, project_id=str(project.id)),
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


async def test_member_cannot_create_work_item(client: httpx.AsyncClient, project: Project) -> None:
    """普通成员创建工作项 → 403。"""
    ctx = await _setup(client, project)
    resp = await _create_item(client, ctx["alice_headers"], str(ctx["alice"].id))  # type: ignore[union-attr]
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"


async def test_full_command_flow_and_audit(client: httpx.AsyncClient, project: Project) -> None:
    """负责人创建+发布 → 指定执行人 start/block/unblock/submit；每次迁移有审计事件。"""
    ctx = await _setup(client, project)
    alice = ctx["alice"]
    bob = ctx["bob"]
    leader_headers = ctx["leader_headers"]
    alice_headers = ctx["alice_headers"]
    bob_headers = ctx["bob_headers"]

    created = await _create_item(
        client, leader_headers, str(alice.id), collaborator_ids=[str(bob.id)]  # type: ignore[union-attr]
    )
    assert created.status_code == 201
    item = created.json()
    assert item["status"] == "DRAFT"
    assert item["version"] == 1
    assert item["assignee"] == {"id": str(alice.id), "display_name": "爱丽丝"}  # type: ignore[union-attr]
    assert item["collaborators"] == [{"id": str(bob.id), "display_name": "鲍勃"}]  # type: ignore[union-attr]
    item_id = item["id"]

    # 主执行人不能在 DRAFT 直接 start（8.1：须先由负责人发布）
    early = await client.post(
        f"/api/v1/work-items/{item_id}/start", json={"version": 1}, headers=alice_headers
    )
    assert early.status_code == 409
    assert early.json()["code"] == "WORK_ITEM_INVALID_TRANSITION"

    # 非负责人不能发布
    publish_forbidden = await client.post(
        f"/api/v1/work-items/{item_id}/publish", json={"version": 1}, headers=alice_headers
    )
    assert publish_forbidden.status_code == 403

    # 负责人发布 → READY
    published = await client.post(
        f"/api/v1/work-items/{item_id}/publish", json={"version": 1}, headers=leader_headers
    )
    assert published.status_code == 200
    assert published.json()["status"] == "READY"
    assert published.json()["version"] == 2

    # 非主执行人不能 start
    start_forbidden = await client.post(
        f"/api/v1/work-items/{item_id}/start", json={"version": 2}, headers=bob_headers
    )
    assert start_forbidden.status_code == 403

    # 开发文档前置（设计 2026-07-30 §4.3）：负责人豁免后放行 start
    waived = await client.post(
        f"/api/v1/work-items/{item_id}/dev-doc/waive", json={}, headers=leader_headers
    )
    assert waived.status_code == 200, waived.text

    # 主执行人 start → IN_PROGRESS
    started = await client.post(
        f"/api/v1/work-items/{item_id}/start", json={"version": 2}, headers=alice_headers
    )
    assert started.status_code == 200
    assert started.json()["status"] == "IN_PROGRESS"
    assert started.json()["version"] == 3

    # block / unblock / submit（T4.4 起：submit 前须已存在交付物）
    blocked = await client.post(
        f"/api/v1/work-items/{item_id}/block", json={"version": 3}, headers=alice_headers
    )
    assert blocked.json()["status"] == "BLOCKED"
    unblocked = await client.post(
        f"/api/v1/work-items/{item_id}/unblock", json={"version": 4}, headers=alice_headers
    )
    assert unblocked.json()["status"] == "IN_PROGRESS"
    delivered = await client.post(
        f"/api/v1/work-items/{item_id}/deliverables",
        json={"type": "text", "content": "交付说明"},
        headers=alice_headers,
    )
    assert delivered.status_code == 201, delivered.text
    submitted = await client.post(
        f"/api/v1/work-items/{item_id}/submit", json={"version": 5}, headers=alice_headers
    )
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "IN_REVIEW"  # submit 只推进到 IN_REVIEW（阶段 4 接审核）

    # 每次状态迁移均有审计事件（8.1 + 原则 5）
    async with async_session_factory() as session:
        events = (await session.execute(select(AuditEvent))).scalars().all()
    actions = [e.action for e in events if str(e.target_id) == item_id]
    for expected in (
        "work_item.created",
        "work_item.published",
        "work_item.started",
        "work_item.blocked",
        "work_item.unblocked",
        "work_item.submitted",
    ):
        assert expected in actions, f"缺少审计事件 {expected}"
    start_event = next(e for e in events if e.action == "work_item.started")
    assert start_event.before == {"status": "READY"}
    assert start_event.after == {"status": "IN_PROGRESS"}


async def test_leader_cancels_work_item(client: httpx.AsyncClient, project: Project) -> None:
    """取消仅负责人；BLOCKED 状态不可取消（8.1）。"""
    ctx = await _setup(client, project)
    alice = ctx["alice"]
    created = await _create_item(client, ctx["leader_headers"], str(alice.id))  # type: ignore[union-attr]
    item = created.json()

    # 成员不能取消
    forbidden = await client.post(
        f"/api/v1/work-items/{item['id']}/cancel", json={"version": 1}, headers=ctx["alice_headers"]
    )
    assert forbidden.status_code == 403

    # 负责人从 DRAFT 取消 → CANCELLED
    cancelled = await client.post(
        f"/api/v1/work-items/{item['id']}/cancel", json={"version": 1}, headers=ctx["leader_headers"]
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"


async def test_list_work_items_full_and_filtered(client: httpx.AsyncClient, project: Project) -> None:
    """任何成员可查全量列表；支持 assignee_id / status / due 区间过滤；返回摘要字段。"""
    ctx = await _setup(client, project)
    alice = ctx["alice"]
    bob = ctx["bob"]
    lh = ctx["leader_headers"]
    await _create_item(client, lh, str(alice.id), title="任务A")  # type: ignore[union-attr]
    await _create_item(
        client, lh, str(bob.id), title="任务B", due_at="2026-09-01T00:00:00Z"  # type: ignore[union-attr]
    )

    resp = await client.get("/api/v1/work-items", headers=ctx["alice_headers"])
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 2
    for it in items:
        assert set(it) == {
            "id", "title", "status", "priority", "assignee", "due_at",
            "version", "created_at", "updated_at",
        }

    by_assignee = await client.get(
        f"/api/v1/work-items?assignee_id={alice.id}", headers=ctx["alice_headers"]  # type: ignore[union-attr]
    )
    assert [i["title"] for i in by_assignee.json()] == ["任务A"]

    by_status = await client.get("/api/v1/work-items?status=READY", headers=ctx["alice_headers"])
    assert by_status.json() == []

    by_due = await client.get(
        "/api/v1/work-items?due_from=2026-08-15T00:00:00Z&due_to=2026-10-01T00:00:00Z",
        headers=ctx["alice_headers"],
    )
    assert [i["title"] for i in by_due.json()] == ["任务B"]


async def test_stale_version_returns_409(client: httpx.AsyncClient, project: Project) -> None:
    """过期 version → 409 WORK_ITEM_VERSION_CONFLICT（命令与 PATCH 均生效）。"""
    ctx = await _setup(client, project)
    alice = ctx["alice"]
    lh = ctx["leader_headers"]
    item = (await _create_item(client, lh, str(alice.id))).json()  # type: ignore[union-attr]

    await client.post(
        f"/api/v1/work-items/{item['id']}/publish", json={"version": 1}, headers=lh
    )
    # 用旧 version 再发布/修改 → 409
    stale_cmd = await client.post(
        f"/api/v1/work-items/{item['id']}/publish", json={"version": 1}, headers=lh
    )
    assert stale_cmd.status_code == 409
    assert stale_cmd.json()["code"] == "WORK_ITEM_VERSION_CONFLICT"

    stale_patch = await client.patch(
        f"/api/v1/work-items/{item['id']}",
        json={"version": 1, "title": "改名"},
        headers=lh,
    )
    assert stale_patch.status_code == 409
    assert stale_patch.json()["code"] == "WORK_ITEM_VERSION_CONFLICT"


async def test_idempotent_start_replays_once(client: httpx.AsyncClient, project: Project) -> None:
    """同一 Idempotency-Key 重复 start：只产生一次状态变化和一条审计事件。"""
    ctx = await _setup(client, project)
    alice = ctx["alice"]
    lh = ctx["leader_headers"]
    ah = ctx["alice_headers"]
    item = (await _create_item(client, lh, str(alice.id))).json()  # type: ignore[union-attr]
    await client.post(f"/api/v1/work-items/{item['id']}/publish", json={"version": 1}, headers=lh)
    # 开发文档前置（设计 2026-07-30 §4.3）：负责人豁免后放行 start
    waived = await client.post(
        f"/api/v1/work-items/{item['id']}/dev-doc/waive", json={}, headers=lh
    )
    assert waived.status_code == 200, waived.text

    headers = {**ah, "Idempotency-Key": "idem-start-0001"}
    r1 = await client.post(
        f"/api/v1/work-items/{item['id']}/start", json={"version": 2}, headers=headers
    )
    r2 = await client.post(
        f"/api/v1/work-items/{item['id']}/start", json={"version": 2}, headers=headers
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.headers.get("Idempotency-Replayed") == "true"
    assert r2.json() == r1.json()
    assert r1.json()["status"] == "IN_PROGRESS"
    assert r1.json()["version"] == 3

    async with async_session_factory() as session:
        events = (
            (await session.execute(select(AuditEvent).where(AuditEvent.action == "work_item.started")))
            .scalars()
            .all()
        )
    assert len(events) == 1  # 只产生一条审计事件


async def test_patch_updates_fields_and_reassigns_with_audit(
    client: httpx.AsyncClient, project: Project
) -> None:
    """负责人 PATCH 改内容/主执行人/协作者（携带 version）；assignee 变化必留痕。"""
    ctx = await _setup(client, project)
    alice = ctx["alice"]
    bob = ctx["bob"]
    lh = ctx["leader_headers"]
    item = (
        await _create_item(client, lh, str(alice.id))  # type: ignore[union-attr]
    ).json()

    # 普通成员不能 PATCH
    forbidden = await client.patch(
        f"/api/v1/work-items/{item['id']}",
        json={"version": 1, "title": "越权"},
        headers=ctx["alice_headers"],
    )
    assert forbidden.status_code == 403

    patched = await client.patch(
        f"/api/v1/work-items/{item['id']}",
        json={
            "version": 1,
            "title": "RAG 工作项 v2",
            "assignee_id": str(bob.id),  # type: ignore[union-attr]
            "collaborator_ids": [str(alice.id)],  # type: ignore[union-attr]
            "due_at": "2026-08-15T00:00:00Z",
        },
        headers=lh,
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["title"] == "RAG 工作项 v2"
    assert body["assignee"]["id"] == str(bob.id)  # type: ignore[union-attr]
    assert body["collaborators"][0]["id"] == str(alice.id)  # type: ignore[union-attr]
    assert body["version"] == 2

    # assignee 变化在审计事件中留痕（为"历史负责人完整可查"打底）
    async with async_session_factory() as session:
        events = (
            (await session.execute(select(AuditEvent).where(AuditEvent.action == "work_item.updated")))
            .scalars()
            .all()
        )
    assert len(events) == 1
    event = events[0]
    assert event.before["assignee_id"] == str(alice.id)  # type: ignore[union-attr]
    assert event.after["assignee_id"] == str(bob.id)  # type: ignore[union-attr]


async def test_get_work_item_detail(client: httpx.AsyncClient, project: Project) -> None:
    """任何成员可看详情；序列化含完整字段。"""
    ctx = await _setup(client, project)
    alice = ctx["alice"]
    item = (await _create_item(client, ctx["leader_headers"], str(alice.id))).json()  # type: ignore[union-attr]

    resp = await client.get(f"/api/v1/work-items/{item['id']}", headers=ctx["bob_headers"])
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "id", "title", "description", "acceptance_criteria", "priority", "status",
        "assignee", "collaborators", "due_at", "version", "created_at", "updated_at",
    }
    assert body["description"] == "实现 RAG"

    missing = await client.get(
        "/api/v1/work-items/00000000-0000-0000-0000-000000000000", headers=ctx["bob_headers"]
    )
    assert missing.status_code == 404


async def test_member_workload_in_members_summary(
    client: httpx.AsyncClient, project: Project
) -> None:
    """GET /members 的 active_work_items 反映当前负载（发布后计入）。"""
    ctx = await _setup(client, project)
    alice = ctx["alice"]
    lh = ctx["leader_headers"]
    item = (await _create_item(client, lh, str(alice.id))).json()  # type: ignore[union-attr]
    await client.post(f"/api/v1/work-items/{item['id']}/publish", json={"version": 1}, headers=lh)

    resp = await client.get("/api/v1/members", headers=ctx["bob_headers"])
    alice_summary = next(m for m in resp.json() if m["username"] == "alice")
    assert alice_summary["active_work_items"] == 1
