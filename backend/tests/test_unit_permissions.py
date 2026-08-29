"""权限策略拒绝分支的单元测试。

覆盖以下拒绝分支：
- 工作项执行命令：负责人（非主执行人）与普通成员均不能 start/block/unblock/submit；
- 协作请求 cancel 的"双方均可"边界：无关第三方被拒；
- 转派审批时点目标成员已被禁用 → 422（审批时重新校验活跃状态）；
- 协作回传引用文件：上传人与工作项无关 → 403；
- 协作级 DDL 变更：与协作无关的第三方成员被拒。
"""

import httpx

from app.domains.project.models import Project, ProjectMember
from tests.helpers_t6a import (
    command_collaboration,
    command_work_item,
    create_collaboration,
    create_deadline_change,
    create_transfer,
    create_work_item,
    make_ctx,
    publish_work_item,
    storage,  # noqa: F401  # fixture：注入临时目录存储 Provider
    upload_file,
)


async def test_leader_cannot_run_assignee_commands(
    client: httpx.AsyncClient, project: Project
) -> None:
    """负责人不是主执行人时，start/block/unblock/submit 一律返回 403。"""
    ctx = await make_ctx(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    leader_headers = ctx["leader_headers"]

    item = await create_work_item(client, leader_headers, alice.id)  # type: ignore[arg-type]
    published = await publish_work_item(client, leader_headers, item)
    item_id = published["id"]

    resp = await command_work_item(client, leader_headers, item_id, "start", 2)  # type: ignore[arg-type]
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"

    waived = await client.post(
        f"/api/v1/work-items/{item_id}/dev-doc/waive", json={}, headers=leader_headers  # type: ignore[arg-type]
    )
    assert waived.status_code == 200, waived.text

    started = await command_work_item(client, ctx["alice_headers"], item_id, "start", 2)  # type: ignore[arg-type]
    assert started.status_code == 200
    blocked = await command_work_item(client, ctx["alice_headers"], item_id, "block", 3)  # type: ignore[arg-type]
    assert blocked.status_code == 200

    resp = await command_work_item(client, leader_headers, item_id, "unblock", 4)  # type: ignore[arg-type]
    assert resp.status_code == 403
    unblocked = await command_work_item(client, ctx["alice_headers"], item_id, "unblock", 4)  # type: ignore[arg-type]
    assert unblocked.status_code == 200

    resp = await command_work_item(client, leader_headers, item_id, "block", 5)  # type: ignore[arg-type]
    assert resp.status_code == 403
    resp = await command_work_item(client, leader_headers, item_id, "submit", 5)  # type: ignore[arg-type]
    assert resp.status_code == 403


async def test_non_assignee_member_cannot_run_progress_commands(
    client: httpx.AsyncClient, project: Project
) -> None:
    """普通成员不是主执行人时，block/unblock/submit 一律返回 403。"""
    ctx = await make_ctx(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    leader_headers = ctx["leader_headers"]
    alice_headers = ctx["alice_headers"]
    bob_headers = ctx["bob_headers"]

    item = await create_work_item(client, leader_headers, alice.id)  # type: ignore[arg-type]
    await publish_work_item(client, leader_headers, item)
    item_id = item["id"]
    waived = await client.post(
        f"/api/v1/work-items/{item_id}/dev-doc/waive", json={}, headers=leader_headers  # type: ignore[arg-type]
    )
    assert waived.status_code == 200, waived.text
    started = await command_work_item(client, alice_headers, item_id, "start", 2)  # type: ignore[arg-type]
    assert started.status_code == 200, started.text

    resp = await command_work_item(client, bob_headers, item_id, "block", 3)  # type: ignore[arg-type]
    assert resp.status_code == 403
    resp = await command_work_item(client, bob_headers, item_id, "submit", 3)  # type: ignore[arg-type]
    assert resp.status_code == 403

    await command_work_item(client, alice_headers, item_id, "block", 3)  # type: ignore[arg-type]
    resp = await command_work_item(client, bob_headers, item_id, "unblock", 4)  # type: ignore[arg-type]
    assert resp.status_code == 403


async def test_collaboration_cancel_rejects_unrelated_third_party(
    client: httpx.AsyncClient, project: Project
) -> None:
    """cancel 仅协作双方可执行，无关成员返回 403。"""
    ctx = await make_ctx(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    leader_headers = ctx["leader_headers"]
    item = await create_work_item(client, leader_headers, alice.id)  # type: ignore[arg-type]
    await publish_work_item(client, leader_headers, item)
    collab = await create_collaboration(client, ctx["alice_headers"], item["id"], bob.id)  # type: ignore[arg-type]

    resp = await command_collaboration(client, ctx["dave_headers"], collab["id"], "cancel", 1)  # type: ignore[arg-type]
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"

    resp = await command_collaboration(client, leader_headers, collab["id"], "cancel", 1)  # type: ignore[arg-type]
    assert resp.status_code == 403

    cancelled = await command_collaboration(client, ctx["alice_headers"], collab["id"], "cancel", 1)  # type: ignore[arg-type]
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"


async def test_transfer_approve_rejects_deactivated_target_member(
    client: httpx.AsyncClient, project: Project
) -> None:
    """审批时点目标成员已被禁用 → 422（approve 时重新校验活跃状态，避免转给已禁用账号）。

    且整个审批回滚：申请保持 PENDING、主执行人不变。
    """
    ctx = await make_ctx(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    leader_headers = ctx["leader_headers"]
    alice_headers = ctx["alice_headers"]

    item = await create_work_item(client, leader_headers, alice.id)  # type: ignore[arg-type]
    await publish_work_item(client, leader_headers, item)
    created = await create_transfer(client, alice_headers, item["id"], bob.id)  # type: ignore[arg-type]
    assert created.status_code == 201
    req_id = created.json()["id"]

    disabled = await client.patch(
        f"/api/v1/members/{bob.id}", json={"is_active": False}, headers=leader_headers  # type: ignore[arg-type]
    )
    assert disabled.status_code == 200

    resp = await client.post(
        f"/api/v1/transfer-requests/{req_id}/approve",
        json={"version": 1},
        headers=leader_headers,  # type: ignore[arg-type]
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"

    # 确认校验失败没有留下部分写入。
    detail = await client.get(f"/api/v1/transfer-requests/{req_id}", headers=leader_headers)  # type: ignore[arg-type]
    assert detail.json()["status"] == "PENDING"
    assert detail.json()["version"] == 1
    item_after = await client.get(f"/api/v1/work-items/{item['id']}", headers=alice_headers)  # type: ignore[arg-type]
    assert item_after.json()["assignee"]["id"] == str(alice.id)


async def test_collaboration_submit_rejects_file_from_unrelated_uploader(
    client: httpx.AsyncClient, project: Project, storage
) -> None:
    """未关联工作项的文件仅允许与该工作项有关的上传人用于协作回传。"""
    ctx = await make_ctx(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    leader_headers = ctx["leader_headers"]
    alice_headers = ctx["alice_headers"]
    bob_headers = ctx["bob_headers"]

    item = await create_work_item(client, leader_headers, alice.id)  # type: ignore[arg-type]
    await publish_work_item(client, leader_headers, item)
    collab = await create_collaboration(client, alice_headers, item["id"], bob.id)  # type: ignore[arg-type]
    await command_collaboration(client, bob_headers, collab["id"], "accept", 1)  # type: ignore[arg-type]
    await command_collaboration(client, bob_headers, collab["id"], "start", 2)  # type: ignore[arg-type]

    # dave 与工作项无关，他上传的未关联文件不能被引用为回传产物
    uploaded = await upload_file(client, ctx["dave_headers"])  # type: ignore[arg-type]
    assert uploaded.status_code == 201
    resp = await command_collaboration(
        client,
        bob_headers,  # type: ignore[arg-type]
        collab["id"],
        "submit",
        3,
        result_text="回传产物",
        file_id=uploaded.json()["id"],
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"

    uploaded2 = await upload_file(client, alice_headers)  # type: ignore[arg-type]
    assert uploaded2.status_code == 201
    ok = await command_collaboration(
        client,
        bob_headers,  # type: ignore[arg-type]
        collab["id"],
        "submit",
        3,
        result_text="回传产物",
        file_id=uploaded2.json()["id"],
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "SUBMITTED"


async def test_collab_deadline_change_rejects_unrelated_third_party(
    client: httpx.AsyncClient, project: Project
) -> None:
    """协作级 DDL 变更仅协作双方可发起：与协作无关的成员 dave → 403。"""
    ctx = await make_ctx(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    leader_headers = ctx["leader_headers"]
    item = await create_work_item(client, leader_headers, alice.id)  # type: ignore[arg-type]
    await publish_work_item(client, leader_headers, item)
    collab = await create_collaboration(client, ctx["alice_headers"], item["id"], bob.id)  # type: ignore[arg-type]

    resp = await create_deadline_change(
        client,
        ctx["dave_headers"],  # type: ignore[arg-type]
        item["id"],
        "collaboration_request",
        collab["id"],
        "2026-07-31T00:00:00Z",
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"
