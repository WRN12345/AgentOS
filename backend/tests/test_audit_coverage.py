"""审计覆盖测试（18.1 节，T6.1）：关键动作必留痕 + 审计只读。

断言每个关键动作都生成对应 audit_events 记录（动作类型、actor、对象 id 正确）：
- 分配/创建工作项        work_item.created
- 转派申请与审批/驳回    transfer.requested / transfer.approved / transfer.rejected
- 协作请求发起与回传     collaboration.requested / collaboration.submitted
- 交付物提交             deliverable.submitted
- 审核通过与打回         review.approved / review.changes_requested
- DDL 变更申请与审批     deadline_change.requested / deadline_change.approved
- 文件上传与下载         file.uploaded / file.downloaded

另断言审计事件不可通过任何 API 修改或删除（16、18.1 节）。
"""

import httpx

from app.domains.project.models import Project, ProjectMember
from tests.helpers_t6a import (
    audit_events_by_action,
    audit_events_for,
    command_collaboration,
    command_work_item,
    create_collaboration,
    create_deadline_change,
    create_deliverable,
    create_transfer,
    create_work_item,
    make_ctx,
    publish_work_item,
    storage,  # noqa: F401  # fixture：注入临时目录存储 Provider
    upload_file,
)


async def _item_in_progress(
    client: httpx.AsyncClient, ctx: dict[str, object]
) -> dict:
    """创建并推进到 IN_PROGRESS（create v1 → publish v2 → start v3）。"""
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    item = await create_work_item(client, ctx["leader_headers"], alice.id)  # type: ignore[arg-type]
    await publish_work_item(client, ctx["leader_headers"], item)  # type: ignore[arg-type]
    # 开发文档前置（设计 2026-07-30 §4.3）：负责人豁免后放行 start
    waived = await client.post(
        f"/api/v1/work-items/{item['id']}/dev-doc/waive", json={}, headers=ctx["leader_headers"]  # type: ignore[arg-type]
    )
    assert waived.status_code == 200, waived.text
    started = await command_work_item(client, ctx["alice_headers"], item["id"], "start", 2)  # type: ignore[arg-type]
    assert started.status_code == 200
    return item


# ---------- 分配/创建工作项 ----------


async def test_work_item_creation_audited(client: httpx.AsyncClient, project: Project) -> None:
    """负责人创建（分配）工作项 → work_item.created，actor 为负责人、target 为工作项。"""
    ctx = await make_ctx(client, project)
    leader: ProjectMember = ctx["leader"]  # type: ignore[assignment]
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    item = await create_work_item(client, ctx["leader_headers"], alice.id)  # type: ignore[arg-type]

    events = await audit_events_for(item["id"])
    created = [e for e in events if e.action == "work_item.created"]
    assert len(created) == 1
    event = created[0]
    assert event.actor_id == leader.user_id
    assert event.target_type == "work_item"
    assert event.after["assignee_id"] == str(alice.id)
    assert event.after["status"] == "DRAFT"


# ---------- 转派申请与审批 ----------


async def test_transfer_request_and_approval_audited(
    client: httpx.AsyncClient, project: Project
) -> None:
    """转派申请（alice）与审批通过（leader）分别留痕，actor 各自正确。"""
    ctx = await make_ctx(client, project)
    leader: ProjectMember = ctx["leader"]  # type: ignore[assignment]
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    item = await create_work_item(client, ctx["leader_headers"], alice.id)  # type: ignore[arg-type]
    await publish_work_item(client, ctx["leader_headers"], item)  # type: ignore[arg-type]

    created = await create_transfer(client, ctx["alice_headers"], item["id"], bob.id)  # type: ignore[arg-type]
    assert created.status_code == 201
    req_id = created.json()["id"]
    approved = await client.post(
        f"/api/v1/transfer-requests/{req_id}/approve",
        json={"version": 1},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert approved.status_code == 200

    events = await audit_events_for(req_id)
    assert [e.action for e in events] == ["transfer.requested", "transfer.approved"]
    assert events[0].actor_id == alice.user_id
    assert events[0].target_type == "transfer_request"
    assert events[0].after["from_member_id"] == str(alice.id)
    assert events[0].after["to_member_id"] == str(bob.id)
    assert events[1].actor_id == leader.user_id
    assert events[1].after["status"] == "APPROVED"


async def test_transfer_rejection_audited(client: httpx.AsyncClient, project: Project) -> None:
    """负责人驳回转派 → transfer.rejected，actor 为负责人。"""
    ctx = await make_ctx(client, project)
    leader: ProjectMember = ctx["leader"]  # type: ignore[assignment]
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    item = await create_work_item(client, ctx["leader_headers"], alice.id)  # type: ignore[arg-type]
    await publish_work_item(client, ctx["leader_headers"], item)  # type: ignore[arg-type]

    created = await create_transfer(client, ctx["alice_headers"], item["id"], bob.id)  # type: ignore[arg-type]
    req_id = created.json()["id"]
    rejected = await client.post(
        f"/api/v1/transfer-requests/{req_id}/reject",
        json={"version": 1},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert rejected.status_code == 200

    events = await audit_events_for(req_id)
    assert [e.action for e in events] == ["transfer.requested", "transfer.rejected"]
    assert events[1].actor_id == leader.user_id
    assert events[1].after["status"] == "REJECTED"


# ---------- 协作请求发起与回传 ----------


async def test_collaboration_request_and_submit_audited(
    client: httpx.AsyncClient, project: Project
) -> None:
    """发起协作（alice）与回传产物（bob）分别留痕，actor 各自正确。"""
    ctx = await make_ctx(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    item = await create_work_item(client, ctx["leader_headers"], alice.id)  # type: ignore[arg-type]
    await publish_work_item(client, ctx["leader_headers"], item)  # type: ignore[arg-type]

    collab = await create_collaboration(client, ctx["alice_headers"], item["id"], bob.id)  # type: ignore[arg-type]
    collab_id = collab["id"]
    accepted = await command_collaboration(client, ctx["bob_headers"], collab_id, "accept", 1)  # type: ignore[arg-type]
    assert accepted.status_code == 200
    started = await command_collaboration(client, ctx["bob_headers"], collab_id, "start", 2)  # type: ignore[arg-type]
    assert started.status_code == 200
    submitted = await command_collaboration(
        client, ctx["bob_headers"], collab_id, "submit", 3, result_text="回传产物"  # type: ignore[arg-type]
    )
    assert submitted.status_code == 200

    events = await audit_events_for(collab_id)
    actions = [e.action for e in events]
    assert "collaboration.requested" in actions
    assert "collaboration.submitted" in actions
    requested = next(e for e in events if e.action == "collaboration.requested")
    assert requested.actor_id == alice.user_id
    assert requested.target_type == "collaboration_request"
    assert requested.after["requester_id"] == str(alice.id)
    assert requested.after["assignee_id"] == str(bob.id)
    submit = next(e for e in events if e.action == "collaboration.submitted")
    assert submit.actor_id == bob.user_id
    assert submit.before == {"status": "IN_PROGRESS"}
    assert submit.after["status"] == "SUBMITTED"


# ---------- 交付物提交 ----------


async def test_deliverable_submission_audited(client: httpx.AsyncClient, project: Project) -> None:
    """主执行人提交交付物 → deliverable.submitted，target 为交付物 id。"""
    ctx = await make_ctx(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    item = await _item_in_progress(client, ctx)

    deliverable = await create_deliverable(client, ctx["alice_headers"], item["id"])  # type: ignore[arg-type]

    events = await audit_events_for(deliverable["id"])
    assert [e.action for e in events] == ["deliverable.submitted"]
    event = events[0]
    assert event.actor_id == alice.user_id
    assert event.target_type == "deliverable"
    assert event.after["work_item_id"] == item["id"]
    assert event.after["version"] == 1


# ---------- 审核通过与打回 ----------


async def _item_in_review(
    client: httpx.AsyncClient, ctx: dict[str, object]
) -> tuple[dict, dict]:
    """推进到 IN_REVIEW，返回 (item, deliverable)。"""
    item = await _item_in_progress(client, ctx)
    deliverable = await create_deliverable(client, ctx["alice_headers"], item["id"])  # type: ignore[arg-type]
    submitted = await command_work_item(client, ctx["alice_headers"], item["id"], "submit", 3)  # type: ignore[arg-type]
    assert submitted.status_code == 200
    return item, deliverable


async def test_review_approve_and_request_changes_audited(
    client: httpx.AsyncClient, project: Project
) -> None:
    """审核通过 → review.approved；打回 → review.changes_requested，actor 均为负责人。"""
    ctx = await make_ctx(client, project)
    leader: ProjectMember = ctx["leader"]  # type: ignore[assignment]
    leader_headers = ctx["leader_headers"]

    # 打回：IN_REVIEW → IN_PROGRESS
    item, deliverable = await _item_in_review(client, ctx)
    resp = await client.post(
        f"/api/v1/work-items/{item['id']}/reviews",
        json={"deliverable_id": deliverable["id"], "decision": "request_changes", "feedback": "需补充评估"},
        headers=leader_headers,  # type: ignore[arg-type]
    )
    assert resp.status_code == 201, resp.text

    events = await audit_events_for(item["id"])
    changes = [e for e in events if e.action == "review.changes_requested"]
    assert len(changes) == 1
    assert changes[0].actor_id == leader.user_id
    assert changes[0].target_type == "work_item"
    assert changes[0].before == {"status": "IN_REVIEW"}
    assert changes[0].after["status"] == "IN_PROGRESS"

    # 重新交付并提交审核（IN_PROGRESS v5 → IN_REVIEW v6），负责人通过
    deliverable2 = await create_deliverable(client, ctx["alice_headers"], item["id"])  # type: ignore[arg-type]
    submitted = await command_work_item(client, ctx["alice_headers"], item["id"], "submit", 5)  # type: ignore[arg-type]
    assert submitted.status_code == 200
    resp = await client.post(
        f"/api/v1/work-items/{item['id']}/reviews",
        json={"deliverable_id": deliverable2["id"], "decision": "approve", "feedback": "通过"},
        headers=leader_headers,  # type: ignore[arg-type]
    )
    assert resp.status_code == 201, resp.text

    events = await audit_events_for(item["id"])
    approved = [e for e in events if e.action == "review.approved"]
    assert len(approved) == 1
    assert approved[0].actor_id == leader.user_id
    assert approved[0].before == {"status": "IN_REVIEW"}
    assert approved[0].after["status"] == "COMPLETED"


# ---------- DDL 变更申请与审批 ----------


async def test_deadline_change_request_and_approval_audited(
    client: httpx.AsyncClient, project: Project
) -> None:
    """DDL 变更申请（alice）与负责人审批通过分别留痕，actor 各自正确。"""
    ctx = await make_ctx(client, project)
    leader: ProjectMember = ctx["leader"]  # type: ignore[assignment]
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    item = await create_work_item(client, ctx["leader_headers"], alice.id)  # type: ignore[arg-type]
    await publish_work_item(client, ctx["leader_headers"], item)  # type: ignore[arg-type]

    created = await create_deadline_change(
        client, ctx["alice_headers"], item["id"], "work_item", item["id"], "2026-08-15T00:00:00Z"  # type: ignore[arg-type]
    )
    assert created.status_code == 201
    req_id = created.json()["id"]
    approved = await client.post(
        f"/api/v1/deadline-change-requests/{req_id}/approve",
        json={"version": 1},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert approved.status_code == 200

    events = await audit_events_for(req_id)
    assert [e.action for e in events] == [
        "deadline_change.requested",
        "deadline_change.approved",
    ]
    assert events[0].actor_id == alice.user_id
    assert events[0].target_type == "deadline_change_request"
    assert events[0].after["target_type"] == "work_item"
    assert events[0].after["target_id"] == item["id"]
    assert events[1].actor_id == leader.user_id
    assert events[1].after["status"] == "APPROVED"


# ---------- 文件上传与下载 ----------


async def test_file_upload_and_download_audited(
    client: httpx.AsyncClient, project: Project, storage
) -> None:
    """上传（alice）→ file.uploaded；下载（leader）→ file.downloaded，target 均为文件 id。"""
    ctx = await make_ctx(client, project)
    leader: ProjectMember = ctx["leader"]  # type: ignore[assignment]
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    item = await create_work_item(client, ctx["leader_headers"], alice.id)  # type: ignore[arg-type]

    uploaded = await upload_file(client, ctx["alice_headers"], work_item_id=item["id"])  # type: ignore[arg-type]
    assert uploaded.status_code == 201
    file_id = uploaded.json()["id"]
    downloaded = await client.get(
        f"/api/v1/files/{file_id}/download", headers=ctx["leader_headers"]  # type: ignore[arg-type]
    )
    assert downloaded.status_code == 200

    events = await audit_events_for(file_id)
    assert [e.action for e in events] == ["file.uploaded", "file.downloaded"]
    assert events[0].actor_id == alice.user_id
    assert events[0].target_type == "stored_file"
    assert events[0].after["work_item_id"] == item["id"]
    assert events[0].after["sha256"] == uploaded.json()["sha256"]
    assert events[1].actor_id == leader.user_id
    assert events[1].target_type == "stored_file"


# ---------- 审计事件不可修改/删除（18.1 节） ----------


async def test_audit_events_are_immutable_via_api(
    client: httpx.AsyncClient, project: Project
) -> None:
    """审计事件无任何修改/删除入口：对 audit-events 资源的写方法与单条访问一律 404/405。"""
    ctx = await make_ctx(client, project)
    leader: ProjectMember = ctx["leader"]  # type: ignore[assignment]
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    leader_headers = ctx["leader_headers"]

    item = await create_work_item(client, leader_headers, alice.id)  # type: ignore[arg-type]
    events = await audit_events_for(item["id"])
    assert len(events) == 1
    event_id = str(events[0].id)

    # 集合资源：只允许 GET；写方法一律 405
    for method in ("post", "put", "patch", "delete"):
        resp = await client.request(
            method.upper(),
            "/api/v1/audit-events",
            json={"action": "forged"},
            headers=leader_headers,  # type: ignore[arg-type]
        )
        assert resp.status_code == 405, f"{method} /audit-events 应 405，实际 {resp.status_code}"

    # 单条资源：不存在任何路由（含 GET），任何方法一律 404/405
    for method in ("get", "post", "put", "patch", "delete"):
        resp = await client.request(
            method.upper(),
            f"/api/v1/audit-events/{event_id}",
            json={"action": "forged"},
            headers=leader_headers,  # type: ignore[arg-type]
        )
        assert resp.status_code in (404, 405), (
            f"{method} /audit-events/{{id}} 应 404/405，实际 {resp.status_code}"
        )

    # 尝试后事件原样仍在、内容未被篡改
    remaining = await audit_events_by_action("work_item.created")
    assert len(remaining) == 1
    assert str(remaining[0].id) == event_id
    assert remaining[0].after["assignee_id"] == str(alice.id)
