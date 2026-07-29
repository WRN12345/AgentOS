"""DDL 影响规则单元测试补充（7.4 节，T6.1）。

只补既有 test_deadlines_api 未覆盖的边界：
- 协作级自动生效的边界：新协作 DDL 恰等于主任务 DDL（≤ 取等）→ 自动生效；
- 主任务无 DDL 时协作级变更一律自动生效；
- 负责人本人可发起主任务级变更（非主执行人但角色允许）；
- 协作级待审批变更被发起人取消后协作 DDL 不变、可再次发起；
- 影响分析的受影响协作清单排除已结束（终态）协作请求；
- 唯一待审批约束仅针对主任务级：协作级可同时存在多个待审批申请。
"""

import httpx

from app.domains.project.models import Project, ProjectMember
from tests.helpers_t6a import (
    command_collaboration,
    create_collaboration,
    create_deadline_change,
    create_work_item,
    make_ctx,
    publish_work_item,
)


async def _get_collab(
    client: httpx.AsyncClient, headers: dict[str, str], item_id: str, collab_id: str
) -> dict:
    resp = await client.get(
        f"/api/v1/work-items/{item_id}/collaboration-requests", headers=headers
    )
    assert resp.status_code == 200, resp.text
    return next(r for r in resp.json() if r["id"] == collab_id)


async def test_collab_due_equal_to_work_item_due_auto_approved(
    client: httpx.AsyncClient, project: Project
) -> None:
    """新协作 DDL 恰等于主任务 DDL（≤ 边界）：不影响主任务 DDL，直接确认生效（7.4 节）。"""
    ctx = await make_ctx(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    leader_headers = ctx["leader_headers"]
    alice_headers = ctx["alice_headers"]

    item = await create_work_item(client, leader_headers, alice.id, due_at="2026-08-01T00:00:00Z")  # type: ignore[arg-type]
    await publish_work_item(client, leader_headers, item)
    collab = await create_collaboration(client, alice_headers, item["id"], bob.id, due_at="2026-07-30T00:00:00Z")  # type: ignore[arg-type]

    # 07-30 → 08-01（与主任务 DDL 相等）
    created = await create_deadline_change(
        client, alice_headers, item["id"], "collaboration_request", collab["id"], "2026-08-01T00:00:00Z"  # type: ignore[arg-type]
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "APPROVED"
    assert body["impact_analysis"]["exceeds_work_item_due"] is False

    # 协作 DDL 直接生效；负责人无待审批事项
    updated = await _get_collab(client, alice_headers, item["id"], collab["id"])  # type: ignore[arg-type]
    assert updated["due_at"].startswith("2026-08-01")
    approvals = await client.get("/api/v1/approvals", headers=leader_headers)  # type: ignore[arg-type]
    assert approvals.json() == []


async def test_collab_change_auto_approved_when_work_item_has_no_due(
    client: httpx.AsyncClient, project: Project
) -> None:
    """主任务无 DDL：协作级变更无论新 DDL 多晚都直接确认生效（7.4 节），无需负责人审批。"""
    ctx = await make_ctx(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    leader_headers = ctx["leader_headers"]
    alice_headers = ctx["alice_headers"]

    item = await create_work_item(client, leader_headers, alice.id, due_at=None)  # type: ignore[arg-type]
    await publish_work_item(client, leader_headers, item)
    collab = await create_collaboration(client, alice_headers, item["id"], bob.id, due_at="2026-07-30T00:00:00Z")  # type: ignore[arg-type]

    # 新协作 DDL 很晚，但主任务无 DDL → 不构成影响
    created = await create_deadline_change(
        client, alice_headers, item["id"], "collaboration_request", collab["id"], "2027-01-01T00:00:00Z"  # type: ignore[arg-type]
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "APPROVED"
    # 主任务无 DDL：影响分析中 exceeds_work_item_due 为 False，work_item.due_at 为 None
    assert body["impact_analysis"]["exceeds_work_item_due"] is False
    assert body["impact_analysis"]["work_item"]["due_at"] is None

    updated = await _get_collab(client, alice_headers, item["id"], collab["id"])  # type: ignore[arg-type]
    assert updated["due_at"].startswith("2027-01-01")
    approvals = await client.get("/api/v1/approvals", headers=leader_headers)  # type: ignore[arg-type]
    assert approvals.json() == []


async def test_leader_can_initiate_main_level_change(
    client: httpx.AsyncClient, project: Project
) -> None:
    """主任务 DDL 的任何修改都必须由负责人批准（7.4 节）：负责人本人可发起，且仍走审批流。"""
    ctx = await make_ctx(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    leader_headers = ctx["leader_headers"]
    alice_headers = ctx["alice_headers"]

    item = await create_work_item(client, leader_headers, alice.id, due_at="2026-08-01T00:00:00Z")  # type: ignore[arg-type]
    published = await publish_work_item(client, leader_headers, item)

    # 负责人（非主执行人）发起主任务级变更：允许，落 PENDING_APPROVAL
    created = await create_deadline_change(
        client, leader_headers, item["id"], "work_item", item["id"], "2026-08-15T00:00:00Z"  # type: ignore[arg-type]
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "PENDING_APPROVAL"
    assert body["requested_by"]["id"] == str(ctx["leader"].id)  # type: ignore[union-attr]

    # 批准前 DDL 不变；负责人审批通过后生效
    before = await client.get(f"/api/v1/work-items/{item['id']}", headers=alice_headers)  # type: ignore[arg-type]
    assert before.json()["due_at"].startswith("2026-08-01")
    approved = await client.post(
        f"/api/v1/deadline-change-requests/{body['id']}/approve",
        json={"version": 1},
        headers=leader_headers,  # type: ignore[arg-type]
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "APPROVED"
    after = await client.get(f"/api/v1/work-items/{item['id']}", headers=alice_headers)  # type: ignore[arg-type]
    assert after.json()["due_at"].startswith("2026-08-15")
    assert after.json()["version"] == published["version"] + 1


async def test_pending_collab_change_cancel_keeps_due_and_reopenable(
    client: httpx.AsyncClient, project: Project
) -> None:
    """协作级待审批变更：发起人取消 → 协作 DDL 不变；取消后可再次发起。"""
    ctx = await make_ctx(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    leader_headers = ctx["leader_headers"]
    alice_headers = ctx["alice_headers"]

    item = await create_work_item(client, leader_headers, alice.id, due_at="2026-08-01T00:00:00Z")  # type: ignore[arg-type]
    await publish_work_item(client, leader_headers, item)
    collab = await create_collaboration(client, alice_headers, item["id"], bob.id, due_at="2026-07-30T00:00:00Z")  # type: ignore[arg-type]

    # 新 DDL 晚于主任务 DDL → 走审批流
    created = await create_deadline_change(
        client, alice_headers, item["id"], "collaboration_request", collab["id"], "2026-08-10T00:00:00Z"  # type: ignore[arg-type]
    )
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "PENDING_APPROVAL"

    cancelled = await client.post(
        f"/api/v1/deadline-change-requests/{body['id']}/cancel",
        json={"version": 1},
        headers=alice_headers,  # type: ignore[arg-type]
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"

    # 协作 DDL 不变；负责人待审批列表清空
    updated = await _get_collab(client, alice_headers, item["id"], collab["id"])  # type: ignore[arg-type]
    assert updated["due_at"].startswith("2026-07-30")
    assert updated["version"] == 1
    approvals = await client.get("/api/v1/approvals", headers=leader_headers)  # type: ignore[arg-type]
    assert approvals.json() == []

    # 取消后可再次发起同一协作的变更
    again = await create_deadline_change(
        client, alice_headers, item["id"], "collaboration_request", collab["id"], "2026-08-10T00:00:00Z"  # type: ignore[arg-type]
    )
    assert again.status_code == 201


async def test_impact_analysis_excludes_terminal_collaborations(
    client: httpx.AsyncClient, project: Project
) -> None:
    """影响分析的受影响协作清单只含未完成协作：已取消/已拒绝的协作不出现在清单中。"""
    ctx = await make_ctx(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    carol: ProjectMember = ctx["carol"]  # type: ignore[assignment]
    dave: ProjectMember = ctx["dave"]  # type: ignore[assignment]
    leader_headers = ctx["leader_headers"]
    alice_headers = ctx["alice_headers"]
    bob_headers = ctx["bob_headers"]
    carol_headers = ctx["carol_headers"]

    item = await create_work_item(client, leader_headers, alice.id, due_at="2026-08-01T00:00:00Z")  # type: ignore[arg-type]
    await publish_work_item(client, leader_headers, item)
    item_id = item["id"]

    # 三个协作：carol 的进行中（未完成）、bob 的已取消、dave 的已拒绝
    open_collab = await create_collaboration(client, alice_headers, item_id, carol.id, title="进行中的协作")  # type: ignore[arg-type]
    cancelled_collab = await create_collaboration(client, alice_headers, item_id, bob.id, title="已取消的协作")  # type: ignore[arg-type]
    declined_collab = await create_collaboration(client, alice_headers, item_id, dave.id, title="已拒绝的协作")  # type: ignore[arg-type]
    cancelled = await command_collaboration(client, alice_headers, cancelled_collab["id"], "cancel", 1)  # type: ignore[arg-type]
    assert cancelled.status_code == 200
    declined = await command_collaboration(client, ctx["dave_headers"], declined_collab["id"], "decline", 1)  # type: ignore[arg-type]
    assert declined.status_code == 200

    created = await create_deadline_change(
        client, alice_headers, item_id, "work_item", item_id, "2026-08-15T00:00:00Z"  # type: ignore[arg-type]
    )
    assert created.status_code == 201
    analysis = created.json()["impact_analysis"]
    affected_ids = {c["id"] for c in analysis["affected_collaboration_requests"]}
    assert open_collab["id"] in affected_ids
    assert cancelled_collab["id"] not in affected_ids
    assert declined_collab["id"] not in affected_ids


async def test_multiple_pending_collab_level_changes_allowed(
    client: httpx.AsyncClient, project: Project
) -> None:
    """唯一待审批约束仅针对主任务级（17.2 节）：同一工作项可同时存在多个待审批的协作级变更。"""
    ctx = await make_ctx(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    carol: ProjectMember = ctx["carol"]  # type: ignore[assignment]
    leader_headers = ctx["leader_headers"]
    alice_headers = ctx["alice_headers"]

    item = await create_work_item(client, leader_headers, alice.id, due_at="2026-08-01T00:00:00Z")  # type: ignore[arg-type]
    await publish_work_item(client, leader_headers, item)
    collab1 = await create_collaboration(client, alice_headers, item["id"], bob.id, title="协作一")  # type: ignore[arg-type]
    collab2 = await create_collaboration(client, alice_headers, item["id"], carol.id, title="协作二")  # type: ignore[arg-type]

    # 两个协作级变更均超过主任务 DDL → 都进入待审批，互不冲突
    first = await create_deadline_change(
        client, alice_headers, item["id"], "collaboration_request", collab1["id"], "2026-08-10T00:00:00Z"  # type: ignore[arg-type]
    )
    assert first.status_code == 201
    assert first.json()["status"] == "PENDING_APPROVAL"
    second = await create_deadline_change(
        client, alice_headers, item["id"], "collaboration_request", collab2["id"], "2026-08-12T00:00:00Z"  # type: ignore[arg-type]
    )
    assert second.status_code == 201, second.text
    assert second.json()["status"] == "PENDING_APPROVAL"

    # 两笔都出现在负责人待审批聚合中
    approvals = await client.get("/api/v1/approvals", headers=leader_headers)  # type: ignore[arg-type]
    pending_ids = {a["id"] for a in approvals.json() if a["kind"] == "deadline_change"}
    assert pending_ids == {first.json()["id"], second.json()["id"]}
