"""API 集成测试中的错误分支补充。

覆盖点（既有 test_*_api.py 未触达的分支）：
- members：PATCH / PUT capabilities 对不存在成员 → 404；
- work-items：创建/更新指定不存在或已禁用的成员 → 422；
- work-items：六个命令端点对不存在工作项 → 404；
- work-items：BLOCKED 状态取消 → 409；
- transfers：approve / reject 对不存在申请 → 404；
- deadlines：approve 过期版本号 → 409 DEADLINE_CHANGE_VERSION_CONFLICT，
  approve / reject 对不存在申请 → 404；
- reviews：IN_REVIEW 工作项引用不存在交付物 → 404；引用其他工作项的交付物 → 422。
"""

import uuid

import httpx
import pytest
from sqlalchemy import select

from app.domains.project.models import Project, ProjectMember
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member, auth_headers
from tests.helpers_t6b import (
    ALICE_PW,
    BOB_PW,
    LEADER_PW,
    create_main_deadline_change,
    create_published_item,
    create_transfer_request,
    setup_trio,
)

COMMANDS = ("publish", "start", "block", "unblock", "submit", "cancel")


async def test_patch_member_not_found_404(client: httpx.AsyncClient, project: Project) -> None:
    """负责人 PATCH 不存在的成员 → 404。"""
    ctx = await setup_trio(client, project)
    resp = await client.patch(
        f"/api/v1/members/{uuid.uuid4()}",
        json={"display_name": "不存在"},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


async def test_put_capabilities_member_not_found_404(
    client: httpx.AsyncClient, project: Project
) -> None:
    """负责人对不存在成员整体替换能力集 → 404。"""
    ctx = await setup_trio(client, project)
    resp = await client.put(
        f"/api/v1/members/{uuid.uuid4()}/capabilities",
        json={"capabilities": [{"tag": "rag", "proficiency": 3}]},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


async def test_create_work_item_with_unknown_assignee_422(
    client: httpx.AsyncClient, project: Project
) -> None:
    """创建/更新工作项指定不存在的 assignee → 422（参数校验）。"""
    ctx = await setup_trio(client, project)
    leader_headers = ctx["leader_headers"]
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]

    created = await client.post(
        "/api/v1/work-items",
        json={"title": "x", "description": "", "assignee_id": str(uuid.uuid4())},
        headers=leader_headers,  # type: ignore[arg-type]
    )
    assert created.status_code == 422
    assert created.json()["code"] == "VALIDATION_ERROR"

    item = await create_published_item(client, leader_headers, alice.id)  # type: ignore[arg-type]
    patched = await client.patch(
        f"/api/v1/work-items/{item['id']}",
        json={"version": item["version"], "assignee_id": str(uuid.uuid4())},
        headers=leader_headers,  # type: ignore[arg-type]
    )
    assert patched.status_code == 422
    assert patched.json()["code"] == "VALIDATION_ERROR"


async def test_create_work_item_with_disabled_assignee_422(
    client: httpx.AsyncClient, project: Project
) -> None:
    """创建/更新工作项指定已禁用成员 → 422（审批时点之外的创建校验同理）。"""
    ctx = await setup_trio(client, project)
    leader_headers = ctx["leader_headers"]
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]

    # 直接建库禁用 alice（绕过 API，只准备数据）
    async with async_session_factory() as session:
        member = await session.get(ProjectMember, alice.id)
        assert member is not None
        member.is_active = False
        await session.commit()

    created = await client.post(
        "/api/v1/work-items",
        json={"title": "x", "description": "", "assignee_id": str(alice.id)},
        headers=leader_headers,  # type: ignore[arg-type]
    )
    assert created.status_code == 422
    assert created.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("command", COMMANDS)
async def test_command_on_missing_work_item_404(
    client: httpx.AsyncClient, project: Project, command: str
) -> None:
    """publish/start/block/unblock/submit/cancel 对不存在工作项 → 404。"""
    ctx = await setup_trio(client, project)
    resp = await client.post(
        f"/api/v1/work-items/{uuid.uuid4()}/{command}",
        json={"version": 1},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


async def test_cancel_blocked_work_item_409(client: httpx.AsyncClient, project: Project) -> None:
    """BLOCKED 状态执行 cancel 返回 409。"""
    ctx = await setup_trio(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    item = await create_published_item(client, ctx["leader_headers"], alice.id)  # type: ignore[arg-type]

    waived = await client.post(
        f"/api/v1/work-items/{item['id']}/dev-doc/waive",
        json={},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert waived.status_code == 200, waived.text
    started = await client.post(
        f"/api/v1/work-items/{item['id']}/start",
        json={"version": 2},
        headers=ctx["alice_headers"],  # type: ignore[arg-type]
    )
    assert started.status_code == 200, started.text
    blocked = await client.post(
        f"/api/v1/work-items/{item['id']}/block",
        json={"version": 3},
        headers=ctx["alice_headers"],  # type: ignore[arg-type]
    )
    assert blocked.status_code == 200, blocked.text

    cancelled = await client.post(
        f"/api/v1/work-items/{item['id']}/cancel",
        json={"version": 4},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert cancelled.status_code == 409
    assert cancelled.json()["code"] == "WORK_ITEM_INVALID_TRANSITION"


async def test_transfer_approve_reject_not_found_404(
    client: httpx.AsyncClient, project: Project
) -> None:
    """负责人审批不存在的转派申请 → 404。"""
    ctx = await setup_trio(client, project)
    for action in ("approve", "reject"):
        resp = await client.post(
            f"/api/v1/transfer-requests/{uuid.uuid4()}/{action}",
            json={"version": 1},
            headers=ctx["leader_headers"],  # type: ignore[arg-type]
        )
        assert resp.status_code == 404, f"{action} 应返回 404"
        assert resp.json()["code"] == "NOT_FOUND"


async def test_deadline_approve_stale_version_409(
    client: httpx.AsyncClient, project: Project
) -> None:
    """负责人携过期版本号审批 DDL 变更时返回 409 DEADLINE_CHANGE_VERSION_CONFLICT。"""
    ctx = await setup_trio(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    item = await create_published_item(client, ctx["leader_headers"], alice.id)  # type: ignore[arg-type]
    change = await create_main_deadline_change(client, ctx["alice_headers"], item["id"])  # type: ignore[arg-type]

    resp = await client.post(
        f"/api/v1/deadline-change-requests/{change['id']}/approve",
        json={"version": 99},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "DEADLINE_CHANGE_VERSION_CONFLICT"


async def test_deadline_approve_reject_not_found_404(
    client: httpx.AsyncClient, project: Project
) -> None:
    """负责人审批不存在的 DDL 变更申请 → 404。"""
    ctx = await setup_trio(client, project)
    for action in ("approve", "reject"):
        resp = await client.post(
            f"/api/v1/deadline-change-requests/{uuid.uuid4()}/{action}",
            json={"version": 1},
            headers=ctx["leader_headers"],  # type: ignore[arg-type]
        )
        assert resp.status_code == 404, f"{action} 应返回 404"
        assert resp.json()["code"] == "NOT_FOUND"


async def _item_in_review(
    client: httpx.AsyncClient,
    ctx: dict[str, object],
    *,
    title: str = "RAG 工作项",
) -> tuple[str, str]:
    """创建并推进到 IN_REVIEW（含一个 text 交付物），返回 (item_id, deliverable_id)。

    版本轨迹：create v1 → publish v2 → start v3 → submit v4。
    """
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    item = await create_published_item(client, ctx["leader_headers"], alice.id, title=title)  # type: ignore[arg-type]
    waived = await client.post(
        f"/api/v1/work-items/{item['id']}/dev-doc/waive",
        json={},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert waived.status_code == 200, waived.text
    started = await client.post(
        f"/api/v1/work-items/{item['id']}/start",
        json={"version": 2},
        headers=ctx["alice_headers"],  # type: ignore[arg-type]
    )
    assert started.status_code == 200, started.text
    deliverable = await client.post(
        f"/api/v1/work-items/{item['id']}/deliverables",
        json={"type": "text", "content": "交付说明"},
        headers=ctx["alice_headers"],  # type: ignore[arg-type]
    )
    assert deliverable.status_code == 201, deliverable.text
    submitted = await client.post(
        f"/api/v1/work-items/{item['id']}/submit",
        json={"version": 3},
        headers=ctx["alice_headers"],  # type: ignore[arg-type]
    )
    assert submitted.status_code == 200, submitted.text
    return item["id"], deliverable.json()["id"]


async def test_review_deliverable_not_found_404(
    client: httpx.AsyncClient, project: Project
) -> None:
    """IN_REVIEW 工作项引用不存在的交付物时返回 404。"""
    ctx = await setup_trio(client, project)
    item_id, _ = await _item_in_review(client, ctx)

    resp = await client.post(
        f"/api/v1/work-items/{item_id}/reviews",
        json={"deliverable_id": str(uuid.uuid4()), "decision": "approve"},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


async def test_review_deliverable_belongs_to_other_item_422(
    client: httpx.AsyncClient, project: Project
) -> None:
    """IN_REVIEW 工作项引用其他工作项的交付物 → 422 交付物不属于该工作项。"""
    ctx = await setup_trio(client, project)
    item_a_id, _ = await _item_in_review(client, ctx, title="工作项 A")
    _, deliverable_b_id = await _item_in_review(client, ctx, title="工作项 B")

    resp = await client.post(
        f"/api/v1/work-items/{item_a_id}/reviews",
        json={"deliverable_id": deliverable_b_id, "decision": "approve"},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"
