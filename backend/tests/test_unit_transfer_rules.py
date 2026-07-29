"""转派规则单元测试补充（7.3 节，T6.1）。

只补既有 test_transfers_api 未覆盖的边界：
- 主责任转移的权限切换时点：审批前新负责人无权限、审批后旧负责人立即失去
  主执行人权限（start / 发起协作 / 再发起转派），新负责人获得全部权限；
- 连续两次转派（A→B→C）后历史负责人链完整可查（审计事件 + 申请历史）。
"""

import uuid

import httpx
from sqlalchemy import select

from app.domains.audit.models import AuditEvent
from app.domains.project.models import Project, ProjectMember
from app.infrastructure.database.engine import async_session_factory
from tests.helpers_t6a import (
    command_work_item,
    create_collaboration,
    create_transfer,
    create_work_item,
    get_work_item,
    make_ctx,
    publish_work_item,
)


async def _approve(
    client: httpx.AsyncClient, headers: dict[str, str], request_id: str, version: int = 1
) -> dict:
    resp = await client.post(
        f"/api/v1/transfer-requests/{request_id}/approve",
        json={"version": version},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_assignee_permissions_switch_only_after_approval(
    client: httpx.AsyncClient, project: Project
) -> None:
    """主责任在审批时点转移（7.3 节）：审批前 bob 无任何权限；审批后 alice 全部失效、bob 生效。"""
    ctx = await make_ctx(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    carol: ProjectMember = ctx["carol"]  # type: ignore[assignment]
    leader_headers = ctx["leader_headers"]
    alice_headers = ctx["alice_headers"]
    bob_headers = ctx["bob_headers"]

    item = await create_work_item(client, leader_headers, alice.id)  # type: ignore[arg-type]
    published = await publish_work_item(client, leader_headers, item)
    item_id = published["id"]

    created = await create_transfer(client, alice_headers, item_id, bob.id)  # type: ignore[arg-type]
    assert created.status_code == 201
    req_id = created.json()["id"]

    # 审批前：bob（拟接收人）不能 start、不能发起协作、不能发起转派
    resp = await command_work_item(client, bob_headers, item_id, "start", 2)  # type: ignore[arg-type]
    assert resp.status_code == 403
    collab_resp = await client.post(
        f"/api/v1/work-items/{item_id}/collaboration-requests",
        json={"assignee_id": str(carol.id), "title": "整理资料", "goal": "按模板整理"},
        headers=bob_headers,  # type: ignore[arg-type]
    )
    assert collab_resp.status_code == 403
    resp = await create_transfer(client, bob_headers, item_id, carol.id)  # type: ignore[arg-type]
    assert resp.status_code == 403

    # 负责人审批通过 → 主责任转移
    await _approve(client, leader_headers, req_id)  # type: ignore[arg-type]

    # 旧负责人 alice：start / 发起协作 / 再发起转派 全部 403
    resp = await command_work_item(client, alice_headers, item_id, "start", 3)  # type: ignore[arg-type]
    assert resp.status_code == 403
    collab_resp = await client.post(
        f"/api/v1/work-items/{item_id}/collaboration-requests",
        json={"assignee_id": str(carol.id), "title": "整理资料", "goal": "按模板整理"},
        headers=alice_headers,  # type: ignore[arg-type]
    )
    assert collab_resp.status_code == 403
    resp = await create_transfer(client, alice_headers, item_id, carol.id)  # type: ignore[arg-type]
    assert resp.status_code == 403

    # 新负责人 bob：可 start、可发起协作、可再发起转派
    started = await command_work_item(client, bob_headers, item_id, "start", 3)  # type: ignore[arg-type]
    assert started.status_code == 200
    assert started.json()["status"] == "IN_PROGRESS"
    collab_resp = await client.post(
        f"/api/v1/work-items/{item_id}/collaboration-requests",
        json={"assignee_id": str(carol.id), "title": "整理资料", "goal": "按模板整理"},
        headers=bob_headers,  # type: ignore[arg-type]
    )
    assert collab_resp.status_code == 201, collab_resp.text
    resp = await create_transfer(client, bob_headers, item_id, carol.id)  # type: ignore[arg-type]
    assert resp.status_code == 201


async def test_chained_transfers_keep_full_assignee_history(
    client: httpx.AsyncClient, project: Project
) -> None:
    """连续两次转派（alice→bob→carol）：每次主责任转移都留痕，历史负责人链完整可查。"""
    ctx = await make_ctx(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    carol: ProjectMember = ctx["carol"]  # type: ignore[assignment]
    leader_headers = ctx["leader_headers"]
    alice_headers = ctx["alice_headers"]
    bob_headers = ctx["bob_headers"]

    item = await create_work_item(client, leader_headers, alice.id)  # type: ignore[arg-type]
    published = await publish_work_item(client, leader_headers, item)
    item_id = published["id"]

    # 第一次转派 alice → bob
    first = await create_transfer(client, alice_headers, item_id, bob.id)  # type: ignore[arg-type]
    assert first.status_code == 201
    await _approve(client, leader_headers, first.json()["id"])  # type: ignore[arg-type]

    # 第二次转派 bob → carol（由新主执行人发起）
    second = await create_transfer(client, bob_headers, item_id, carol.id)  # type: ignore[arg-type]
    assert second.status_code == 201, second.text
    await _approve(client, leader_headers, second.json()["id"])  # type: ignore[arg-type]

    item_after = await get_work_item(client, bob_headers, item_id)  # type: ignore[arg-type]
    assert item_after["assignee"]["id"] == str(carol.id)

    # 审计链：两条 assignee_changed，from/to 依次推进（历史负责人完整追溯）
    async with async_session_factory() as session:
        events = list(
            (
                await session.execute(
                    select(AuditEvent)
                    .where(
                        AuditEvent.target_id == uuid.UUID(item_id),
                        AuditEvent.action == "work_item.assignee_changed",
                    )
                    .order_by(AuditEvent.created_at)
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 2
    assert events[0].before == {"assignee_id": str(alice.id)}
    assert events[0].after["assignee_id"] == str(bob.id)
    assert events[0].after["transfer_request_id"] == first.json()["id"]
    assert events[1].before == {"assignee_id": str(bob.id)}
    assert events[1].after["assignee_id"] == str(carol.id)
    assert events[1].after["transfer_request_id"] == second.json()["id"]

    # 申请历史：两笔按时间倒序，from/to 链与工作项当前负责人一致
    history = await client.get(
        f"/api/v1/work-items/{item_id}/transfer-requests", headers=alice_headers  # type: ignore[arg-type]
    )
    assert history.status_code == 200
    records = history.json()
    assert [r["id"] for r in records] == [second.json()["id"], first.json()["id"]]
    assert records[0]["from_member"]["id"] == str(bob.id)
    assert records[0]["to_member"]["id"] == str(carol.id)
    assert records[1]["from_member"]["id"] == str(alice.id)
    assert records[1]["to_member"]["id"] == str(bob.id)
    assert all(r["status"] == "APPROVED" for r in records)
