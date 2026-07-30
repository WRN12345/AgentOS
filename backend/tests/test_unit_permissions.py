"""权限策略单元测试补充（16 节"每个 API 用例显式校验"，T6.1）。

只补既有测试未覆盖的拒绝分支：
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
    """负责人不是主执行人时，start/block/unblock/submit 一律 403（主责任唯一，原则 4）。"""
    ctx = await make_ctx(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    leader_headers = ctx["leader_headers"]

    item = await create_work_item(client, leader_headers, alice.id)  # type: ignore[arg-type]
    published = await publish_work_item(client, leader_headers, item)
    item_id = published["id"]

    # READY：负责人不能替主执行人 start
    resp = await command_work_item(client, leader_headers, item_id, "start", 2)  # type: ignore[arg-type]
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"

    # 开发文档前置（设计 2026-07-30 §4.3）：负责人豁免后放行 start
    waived = await client.post(
        f"/api/v1/work-items/{item_id}/dev-doc/waive", json={}, headers=leader_headers  # type: ignore[arg-type]
    )
    assert waived.status_code == 200, waived.text

    # 主执行人正常 start → IN_PROGRESS(v3)
    started = await command_work_item(client, ctx["alice_headers"], item_id, "start", 2)  # type: ignore[arg-type]
    assert started.status_code == 200
    # 主执行人 block → BLOCKED(v4)
    blocked = await command_work_item(client, ctx["alice_headers"], item_id, "block", 3)  # type: ignore[arg-type]
    assert blocked.status_code == 200

    # BLOCKED：负责人不能 unblock
    resp = await command_work_item(client, leader_headers, item_id, "unblock", 4)  # type: ignore[arg-type]
    assert resp.status_code == 403
    # 主执行人 unblock → IN_PROGRESS(v5)
    unblocked = await command_work_item(client, ctx["alice_headers"], item_id, "unblock", 4)  # type: ignore[arg-type]
    assert unblocked.status_code == 200

    # IN_PROGRESS：负责人不能 block / submit
    resp = await command_work_item(client, leader_headers, item_id, "block", 5)  # type: ignore[arg-type]
    assert resp.status_code == 403
    resp = await command_work_item(client, leader_headers, item_id, "submit", 5)  # type: ignore[arg-type]
    assert resp.status_code == 403


async def test_non_assignee_member_cannot_run_progress_commands(
    client: httpx.AsyncClient, project: Project
) -> None:
    """普通成员（非主执行人）block/unblock/submit → 403（既有测试只覆盖过 start）。"""
    ctx = await make_ctx(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    leader_headers = ctx["leader_headers"]
    alice_headers = ctx["alice_headers"]
    bob_headers = ctx["bob_headers"]

    item = await create_work_item(client, leader_headers, alice.id)  # type: ignore[arg-type]
    await publish_work_item(client, leader_headers, item)
    item_id = item["id"]
    # 开发文档前置（设计 2026-07-30 §4.3）：负责人豁免后放行 start
    waived = await client.post(
        f"/api/v1/work-items/{item_id}/dev-doc/waive", json={}, headers=leader_headers  # type: ignore[arg-type]
    )
    assert waived.status_code == 200, waived.text
    started = await command_work_item(client, alice_headers, item_id, "start", 2)  # type: ignore[arg-type]
    assert started.status_code == 200, started.text

    # IN_PROGRESS：bob 不能 block
    resp = await command_work_item(client, bob_headers, item_id, "block", 3)  # type: ignore[arg-type]
    assert resp.status_code == 403
    # bob 不能 submit
    resp = await command_work_item(client, bob_headers, item_id, "submit", 3)  # type: ignore[arg-type]
    assert resp.status_code == 403

    # 主执行人 block → BLOCKED(v4)，bob 不能 unblock
    await command_work_item(client, alice_headers, item_id, "block", 3)  # type: ignore[arg-type]
    resp = await command_work_item(client, bob_headers, item_id, "unblock", 4)  # type: ignore[arg-type]
    assert resp.status_code == 403


async def test_collaboration_cancel_rejects_unrelated_third_party(
    client: httpx.AsyncClient, project: Project
) -> None:
    """cancel 仅协作双方可执行（8.2 节"双方确认取消"）：无关成员 dave → 403。"""
    ctx = await make_ctx(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    leader_headers = ctx["leader_headers"]
    item = await create_work_item(client, leader_headers, alice.id)  # type: ignore[arg-type]
    await publish_work_item(client, leader_headers, item)
    collab = await create_collaboration(client, ctx["alice_headers"], item["id"], bob.id)  # type: ignore[arg-type]

    # 无关成员不能取消
    resp = await command_collaboration(client, ctx["dave_headers"], collab["id"], "cancel", 1)  # type: ignore[arg-type]
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"

    # 负责人但不是协作双方，同样不能取消
    resp = await command_collaboration(client, leader_headers, collab["id"], "cancel", 1)  # type: ignore[arg-type]
    assert resp.status_code == 403

    # 发起人本人可取消（对照）
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

    # 负责人在审批前禁用 bob
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

    # 申请未被推进，主执行人未变
    detail = await client.get(f"/api/v1/transfer-requests/{req_id}", headers=leader_headers)  # type: ignore[arg-type]
    assert detail.json()["status"] == "PENDING"
    assert detail.json()["version"] == 1
    item_after = await client.get(f"/api/v1/work-items/{item['id']}", headers=alice_headers)  # type: ignore[arg-type]
    assert item_after.json()["assignee"]["id"] == str(alice.id)


async def test_collaboration_submit_rejects_file_from_unrelated_uploader(
    client: httpx.AsyncClient, project: Project, storage
) -> None:
    """回传引用文件：文件未关联本工作项且上传人与工作项无关 → 403（16 节）。"""
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

    # 上传人 alice 是主执行人（与工作项有关）：同一请求可通过（对照）
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
