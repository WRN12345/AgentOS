"""最终审核 API 集成测试（T4.5 验收，7.5、8.1、12.5、16、17.2 节）。

覆盖：三种结论驱动正确状态迁移、reviews+审计同事务、权限（仅负责人）、
反馈可见性（仅负责人与主执行人）、幂等键重放、COMPLETED 拒新交付物。
"""

import httpx
from sqlalchemy import select

from app.domains.audit.models import AuditEvent
from app.domains.notifications.models import Notification
from app.domains.project.models import Project, ProjectMember
from app.domains.reviews.models import Review
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member, auth_headers

LEADER_PW = "Leader123!"
ALICE_PW = "Alice123!"
BOB_PW = "Bob123!"
DAVE_PW = "Dave123!"


async def _setup(client: httpx.AsyncClient, project: Project) -> dict[str, object]:
    """leader + alice（主执行人）+ bob（普通成员/协作者）+ dave（无关成员）。"""
    _, leader = await add_member(project, "leader", LEADER_PW, role="leader", display_name="负责人")
    _, alice = await add_member(project, "alice", ALICE_PW, display_name="爱丽丝")
    _, bob = await add_member(project, "bob", BOB_PW, display_name="鲍勃")
    _, dave = await add_member(project, "dave", DAVE_PW, display_name="戴夫")
    return {
        "leader": leader,
        "alice": alice,
        "bob": bob,
        "dave": dave,
        "leader_headers": await auth_headers(client, "leader", LEADER_PW),
        "alice_headers": await auth_headers(client, "alice", ALICE_PW),
        "bob_headers": await auth_headers(client, "bob", BOB_PW),
        "dave_headers": await auth_headers(client, "dave", DAVE_PW),
    }


async def _item_in_review(
    client: httpx.AsyncClient,
    ctx: dict[str, object],
    collaborator_ids: list[str] | None = None,
) -> tuple[str, str]:
    """创建并推进到 IN_REVIEW（含一个 text 交付物），返回 (item_id, deliverable_id)。

    版本轨迹：create v1 → publish v2 → start v3 → submit v4。
    """
    alice = ctx["alice"]
    leader_headers = ctx["leader_headers"]  # type: ignore[assignment]
    alice_headers = ctx["alice_headers"]  # type: ignore[assignment]
    resp = await client.post(
        "/api/v1/work-items",
        json={
            "title": "RAG 工作项",
            "description": "实现 RAG",
            "priority": "high",
            "assignee_id": str(alice.id),  # type: ignore[union-attr]
            "collaborator_ids": collaborator_ids or [],
        },
        headers=leader_headers,
    )
    assert resp.status_code == 201, resp.text
    item_id = resp.json()["id"]
    # 开发文档前置（设计 2026-07-30 §4.3）：负责人豁免后放行 start
    resp = await client.post(
        f"/api/v1/work-items/{item_id}/dev-doc/waive", json={}, headers=leader_headers
    )
    assert resp.status_code == 200, resp.text
    for command, version in (("publish", 1), ("start", 2)):
        headers = leader_headers if command == "publish" else alice_headers
        resp = await client.post(
            f"/api/v1/work-items/{item_id}/{command}", json={"version": version}, headers=headers
        )
        assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"/api/v1/work-items/{item_id}/deliverables",
        json={"type": "text", "content": "交付说明"},
        headers=alice_headers,
    )
    assert resp.status_code == 201, resp.text
    deliverable_id = resp.json()["id"]
    resp = await client.post(
        f"/api/v1/work-items/{item_id}/submit", json={"version": 3}, headers=alice_headers
    )
    assert resp.status_code == 200, resp.text
    return item_id, deliverable_id


async def _review(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    item_id: str,
    payload: dict[str, object],
    extra_headers: dict[str, str] | None = None,
) -> httpx.Response:
    return await client.post(
        f"/api/v1/work-items/{item_id}/reviews",
        json=payload,
        headers={**headers, **(extra_headers or {})},
    )


async def _work_item_status(client: httpx.AsyncClient, headers: dict[str, str], item_id: str) -> str:
    resp = await client.get(f"/api/v1/work-items/{item_id}", headers=headers)
    assert resp.status_code == 200
    return resp.json()["status"]


# ---------- 三种结论的状态迁移与事务性 ----------


async def test_approve_completes_work_item_with_review_and_audit(
    client: httpx.AsyncClient, project: Project
) -> None:
    """approve → IN_REVIEW → COMPLETED；reviews 记录与审计事件同事务落库；通知主执行人。"""
    ctx = await _setup(client, project)
    alice = ctx["alice"]
    item_id, deliverable_id = await _item_in_review(client, ctx)

    resp = await _review(
        client,
        ctx["leader_headers"],  # type: ignore[arg-type]
        item_id,
        {"deliverable_id": deliverable_id, "decision": "approve", "feedback": "做得好"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["decision"] == "approve"
    assert body["work_item_status"] == "COMPLETED"
    assert body["deliverable_version"] == 1
    assert await _work_item_status(client, ctx["alice_headers"], item_id) == "COMPLETED"  # type: ignore[arg-type]

    async with async_session_factory() as session:
        reviews = list((await session.execute(select(Review))).scalars().all())
        assert len(reviews) == 1
        assert str(reviews[0].deliverable_id) == deliverable_id
        assert str(reviews[0].work_item_id) == item_id

        events = list((await session.execute(select(AuditEvent))).scalars().all())
        review_events = [e for e in events if e.action == "review.approved"]
        assert len(review_events) == 1
        assert review_events[0].before == {"status": "IN_REVIEW"}
        assert review_events[0].after["status"] == "COMPLETED"
        assert review_events[0].after["deliverable_version"] == 1

        # 主执行人收到通知；反馈正文不进通知（16 节）
        notifications = list((await session.execute(select(Notification))).scalars().all())
        mine = [n for n in notifications if n.recipient_id == alice.id and n.type == "review.approved"]  # type: ignore[union-attr]
        assert len(mine) == 1
        assert "做得好" not in mine[0].body


async def test_request_changes_returns_to_in_progress_and_requires_feedback(
    client: httpx.AsyncClient, project: Project
) -> None:
    """request_changes → IN_REVIEW → IN_PROGRESS；缺反馈 → 422。"""
    ctx = await _setup(client, project)
    item_id, deliverable_id = await _item_in_review(client, ctx)

    missing = await _review(
        client,
        ctx["leader_headers"],  # type: ignore[arg-type]
        item_id,
        {"deliverable_id": deliverable_id, "decision": "request_changes"},
    )
    assert missing.status_code == 422

    resp = await _review(
        client,
        ctx["leader_headers"],  # type: ignore[arg-type]
        item_id,
        {
            "deliverable_id": deliverable_id,
            "decision": "request_changes",
            "feedback": "请补充评测指标",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["work_item_status"] == "IN_PROGRESS"
    assert await _work_item_status(client, ctx["alice_headers"], item_id) == "IN_PROGRESS"  # type: ignore[arg-type]

    # 修改后可再次提交交付物并重新送审（版本递增、工作项 version 继续推进）
    again = await client.post(
        f"/api/v1/work-items/{item_id}/deliverables",
        json={"type": "text", "content": "第二版"},
        headers=ctx["alice_headers"],  # type: ignore[arg-type]
    )
    assert again.status_code == 201
    assert again.json()["version"] == 2
    resubmitted = await client.post(
        f"/api/v1/work-items/{item_id}/submit",
        json={"version": 5},
        headers=ctx["alice_headers"],  # type: ignore[arg-type]
    )
    assert resubmitted.status_code == 200
    assert resubmitted.json()["status"] == "IN_REVIEW"


async def test_reject_keeps_work_item_in_review(
    client: httpx.AsyncClient, project: Project
) -> None:
    """reject → 拒绝当前交付但保持工作项继续执行（状态保持 IN_REVIEW，7.5 节）。"""
    ctx = await _setup(client, project)
    item_id, deliverable_id = await _item_in_review(client, ctx)

    resp = await _review(
        client,
        ctx["leader_headers"],  # type: ignore[arg-type]
        item_id,
        {"deliverable_id": deliverable_id, "decision": "reject", "feedback": "方向不对"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["work_item_status"] == "IN_REVIEW"
    assert await _work_item_status(client, ctx["alice_headers"], item_id) == "IN_REVIEW"  # type: ignore[arg-type]

    async with async_session_factory() as session:
        events = list((await session.execute(select(AuditEvent))).scalars().all())
        assert any(e.action == "review.rejected" for e in events)


# ---------- 权限与可见性 ----------


async def test_non_leader_cannot_review(client: httpx.AsyncClient, project: Project) -> None:
    """普通成员（含主执行人本人）调用审核接口 → 403（6.1 节）。"""
    ctx = await _setup(client, project)
    item_id, deliverable_id = await _item_in_review(client, ctx)

    for role in ("alice_headers", "bob_headers", "dave_headers"):
        resp = await _review(
            client,
            ctx[role],  # type: ignore[arg-type]
            item_id,
            {"deliverable_id": deliverable_id, "decision": "approve"},
        )
        assert resp.status_code == 403, f"{role} 不应能审核"


async def test_review_requires_in_review_status(
    client: httpx.AsyncClient, project: Project
) -> None:
    """工作项不在 IN_REVIEW 时审核 → 409。"""
    ctx = await _setup(client, project)
    alice = ctx["alice"]
    leader_headers = ctx["leader_headers"]  # type: ignore[assignment]
    resp = await client.post(
        "/api/v1/work-items",
        json={
            "title": "未送审工作项",
            "description": "",
            "assignee_id": str(alice.id),  # type: ignore[union-attr]
        },
        headers=leader_headers,
    )
    item_id = resp.json()["id"]
    resp = await _review(
        client,
        leader_headers,
        item_id,
        {
            "deliverable_id": "00000000-0000-0000-0000-000000000000",
            "decision": "approve",
        },
    )
    assert resp.status_code == 409


async def test_review_feedback_visible_only_to_leader_and_assignee(
    client: httpx.AsyncClient, project: Project
) -> None:
    """反馈正文仅负责人与该工作项主执行人可见（16 节）；协作者与无关成员 403。"""
    ctx = await _setup(client, project)
    bob = ctx["bob"]
    item_id, deliverable_id = await _item_in_review(
        client, ctx, collaborator_ids=[str(bob.id)]  # type: ignore[union-attr]
    )
    resp = await _review(
        client,
        ctx["leader_headers"],  # type: ignore[arg-type]
        item_id,
        {
            "deliverable_id": deliverable_id,
            "decision": "request_changes",
            "feedback": "机密反馈正文",
        },
    )
    assert resp.status_code == 201

    for role in ("leader_headers", "alice_headers"):
        visible = await client.get(
            f"/api/v1/work-items/{item_id}/reviews", headers=ctx[role]  # type: ignore[arg-type]
        )
        assert visible.status_code == 200, f"{role} 应可见"
        assert visible.json()[0]["feedback"] == "机密反馈正文"

    # 协作者（非主执行人）与无关成员均读不到反馈正文
    for role in ("bob_headers", "dave_headers"):
        denied = await client.get(
            f"/api/v1/work-items/{item_id}/reviews", headers=ctx[role]  # type: ignore[arg-type]
        )
        assert denied.status_code == 403, f"{role} 不应读到审核反馈"


# ---------- 幂等与终态约束 ----------


async def test_review_idempotent_replay_applies_once(
    client: httpx.AsyncClient, project: Project
) -> None:
    """同一 Idempotency-Key 重放：不重复落 reviews、不重复推进状态（17.2 节）。"""
    ctx = await _setup(client, project)
    item_id, deliverable_id = await _item_in_review(client, ctx)

    key = {"Idempotency-Key": "review-approve-1"}
    payload = {"deliverable_id": deliverable_id, "decision": "approve"}
    first = await _review(client, ctx["leader_headers"], item_id, payload, key)  # type: ignore[arg-type]
    assert first.status_code == 201, first.text
    replayed = await _review(client, ctx["leader_headers"], item_id, payload, key)  # type: ignore[arg-type]
    assert replayed.status_code == 201
    assert replayed.headers.get("Idempotency-Replayed") == "true"
    assert replayed.json()["id"] == first.json()["id"]

    async with async_session_factory() as session:
        reviews = list((await session.execute(select(Review))).scalars().all())
        assert len(reviews) == 1  # 只生效一次

    # 无幂等键的重复 approve：工作项已 COMPLETED，状态机拒绝 → 409
    again = await _review(client, ctx["leader_headers"], item_id, payload)  # type: ignore[arg-type]
    assert again.status_code == 409


async def test_completed_work_item_rejects_new_deliverable(
    client: httpx.AsyncClient, project: Project
) -> None:
    """approve 后工作项 COMPLETED，不可再提交新交付物版本（T4.5 验收）。"""
    ctx = await _setup(client, project)
    item_id, deliverable_id = await _item_in_review(client, ctx)

    resp = await _review(
        client,
        ctx["leader_headers"],  # type: ignore[arg-type]
        item_id,
        {"deliverable_id": deliverable_id, "decision": "approve"},
    )
    assert resp.status_code == 201

    rejected = await client.post(
        f"/api/v1/work-items/{item_id}/deliverables",
        json={"type": "text", "content": "迟到的交付"},
        headers=ctx["alice_headers"],  # type: ignore[arg-type]
    )
    assert rejected.status_code == 409
    # 历史版本仍可查
    history = await client.get(
        f"/api/v1/work-items/{item_id}/deliverables", headers=ctx["leader_headers"]  # type: ignore[arg-type]
    )
    assert history.status_code == 200
    assert [d["version"] for d in history.json()] == [1]
