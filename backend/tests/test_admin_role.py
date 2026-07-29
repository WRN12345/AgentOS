"""管理员角色（admin）权限矩阵集成测试。

admin 定位：领导用的"查看 + 账号管理"角色——
- 可读全部页面数据（成员/工作项/审批列表/审计/通知/Agent 建议与运行记录）；
- 与 leader 同权管理成员账号（创建/编辑/禁用/能力确认）；
- 不做业务写操作（建工作项/审批/审核/发起协作/提交交付/建议反馈一律 403）；
- 不可被指派（工作项主执行人/协作者、转派目标、协作接收人 → 422，
  文案"管理员不参与工作协作，不能被指派"）。
"""

import uuid

import httpx

from app.domains.project.models import Project, ProjectMember
from tests.conftest import add_member, auth_headers

LEADER_PW = "Leader123!"
ADMIN_PW = "Admin123!"
ALICE_PW = "Alice123!"
BOB_PW = "Bob123!"


async def _make_ctx(client: httpx.AsyncClient, project: Project) -> dict[str, object]:
    """标准场景：leader + admin + alice/bob 两名普通成员及各自请求头。"""
    _, leader = await add_member(project, "leader", LEADER_PW, role="leader", display_name="负责人")
    _, admin = await add_member(project, "admin", ADMIN_PW, role="admin", display_name="管理员")
    _, alice = await add_member(project, "alice", ALICE_PW, display_name="爱丽丝")
    _, bob = await add_member(project, "bob", BOB_PW, display_name="鲍勃")
    return {
        "leader": leader,
        "admin": admin,
        "alice": alice,
        "bob": bob,
        "leader_headers": await auth_headers(client, "leader", LEADER_PW),
        "admin_headers": await auth_headers(client, "admin", ADMIN_PW),
        "alice_headers": await auth_headers(client, "alice", ALICE_PW),
    }


async def _create_work_item(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    assignee_id: uuid.UUID,
    **extra: object,
) -> dict:
    """负责人创建工作项（DRAFT，version=1）。"""
    payload: dict[str, object] = {
        "title": "RAG 工作项",
        "description": "实现 RAG",
        "priority": "high",
        "assignee_id": str(assignee_id),
        **extra,
    }
    resp = await client.post("/api/v1/work-items", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------- 成员账号管理：admin 与 leader 同权 ----------


async def test_admin_can_manage_member_accounts(
    client: httpx.AsyncClient, project: Project
) -> None:
    """admin 可创建/编辑/禁用成员、维护并确认能力（与 leader 同权）。"""
    ctx = await _make_ctx(client, project)
    admin_headers = ctx["admin_headers"]
    assert isinstance(admin_headers, dict)

    # 创建成员 → 201，初始密码仅返回一次
    created = await client.post(
        "/api/v1/members",
        json={
            "username": "carol",
            "password": "Carol123!",
            "display_name": "卡罗尔",
            "role": "member",
        },
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    carol = created.json()
    assert carol["initial_password"] == "Carol123!"

    # 编辑资料 → 200
    patched = await client.patch(
        f"/api/v1/members/{carol['id']}",
        json={"display_name": "卡罗尔·陈", "weekly_available_hours": 20},
        headers=admin_headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["display_name"] == "卡罗尔·陈"

    # 维护能力并确认 → 200，confirmed 翻转
    caps = await client.put(
        f"/api/v1/members/{carol['id']}/capabilities",
        json={"capabilities": [{"tag": "RAG", "proficiency": 4}], "confirm": True},
        headers=admin_headers,
    )
    assert caps.status_code == 200, caps.text
    assert caps.json()["capabilities"][0]["confirmed"] is True

    # 禁用 → 200，账号立即无法登录
    disabled = await client.patch(
        f"/api/v1/members/{carol['id']}", json={"is_active": False}, headers=admin_headers
    )
    assert disabled.status_code == 200, disabled.text
    login = await client.post(
        "/api/v1/auth/login", json={"username": "carol", "password": "Carol123!"}
    )
    assert login.status_code == 403
    assert login.json()["code"] == "USER_DISABLED"


async def test_admin_disable_self_follows_leader_rules(
    client: httpx.AsyncClient, project: Project
) -> None:
    """admin 停用自己与现有 leader 规则对齐（允许），停用后账号无法登录。"""
    ctx = await _make_ctx(client, project)
    admin: ProjectMember = ctx["admin"]  # type: ignore[assignment]
    admin_headers = ctx["admin_headers"]
    assert isinstance(admin_headers, dict)

    resp = await client.patch(
        f"/api/v1/members/{admin.id}", json={"is_active": False}, headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    login = await client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": ADMIN_PW}
    )
    assert login.status_code == 403


# ---------- 只读访问：admin 可读全部页面数据 ----------


async def test_admin_read_access(
    client: httpx.AsyncClient, project: Project
) -> None:
    """admin 可读成员/工作项/审批列表/审计/通知/Agent 建议与运行记录。"""
    ctx = await _make_ctx(client, project)
    admin_headers = ctx["admin_headers"]
    leader_headers = ctx["leader_headers"]
    alice_headers = ctx["alice_headers"]
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    assert isinstance(admin_headers, dict) and isinstance(leader_headers, dict)

    # 造一条待审批数据：alice 对自己的工作项发起转派（PENDING）
    item = await _create_work_item(client, leader_headers, alice.id)  # type: ignore[arg-type]
    transfer = await client.post(
        f"/api/v1/work-items/{item['id']}/transfer-requests",
        json={
            "to_member_id": str(bob.id),
            "reason": "超出我的能力范围",
            "impact_note": "DDL 不变",
        },
        headers=alice_headers,  # type: ignore[arg-type]
    )
    assert transfer.status_code == 201, transfer.text

    # 各只读列表一律 200
    for path in (
        "/api/v1/members",
        "/api/v1/work-items",
        "/api/v1/audit-events",
        "/api/v1/notifications",
        "/api/v1/agent-suggestions",
        "/api/v1/agent-runs",
    ):
        resp = await client.get(path, headers=admin_headers)
        assert resp.status_code == 200, f"GET {path} 应 200，实际 {resp.status_code}"

    # GET /approvals：admin 与 leader 一样能看到待审批数据（只读）
    approvals = await client.get("/api/v1/approvals", headers=admin_headers)
    assert approvals.status_code == 200
    assert any(a["id"] == transfer.json()["id"] for a in approvals.json())
    # 普通成员仍返回空列表（不 403，T3.5 语义不变）
    member_approvals = await client.get("/api/v1/approvals", headers=alice_headers)  # type: ignore[arg-type]
    assert member_approvals.status_code == 200
    assert member_approvals.json() == []


# ---------- 业务写操作：admin 一律 403 ----------


async def test_admin_cannot_write_business(
    client: httpx.AsyncClient, project: Project
) -> None:
    """admin 建工作项/命令/审批/审核/发起协作/提交交付/写建议反馈 → 403。"""
    ctx = await _make_ctx(client, project)
    admin_headers = ctx["admin_headers"]
    leader_headers = ctx["leader_headers"]
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    assert isinstance(admin_headers, dict) and isinstance(leader_headers, dict)

    item = await _create_work_item(client, leader_headers, alice.id)

    # 创建/编辑工作项 → 403
    created = await client.post(
        "/api/v1/work-items",
        json={"title": "越权工作项", "priority": "low", "assignee_id": str(alice.id)},
        headers=admin_headers,
    )
    assert created.status_code == 403
    patched = await client.patch(
        f"/api/v1/work-items/{item['id']}",
        json={"version": item["version"], "title": "越权修改"},
        headers=admin_headers,
    )
    assert patched.status_code == 403

    # 状态命令（publish 为 leader 专属）→ 403
    published = await client.post(
        f"/api/v1/work-items/{item['id']}/publish",
        json={"version": item["version"]},
        headers=admin_headers,
    )
    assert published.status_code == 403

    # 审批（转派）→ 403：先由 alice 发起一条 PENDING 转派
    transfer = await client.post(
        f"/api/v1/work-items/{item['id']}/transfer-requests",
        json={"to_member_id": str(bob.id), "reason": "r", "impact_note": "i"},
        headers=ctx["alice_headers"],  # type: ignore[arg-type]
    )
    assert transfer.status_code == 201, transfer.text
    approved = await client.post(
        f"/api/v1/transfer-requests/{transfer.json()['id']}/approve",
        json={"version": transfer.json()["version"]},
        headers=admin_headers,
    )
    assert approved.status_code == 403

    # DDL 变更审批（leader 专属路由）→ 403
    ddl = await client.post(
        f"/api/v1/work-items/{item['id']}/deadline-change-requests",
        json={
            "target_type": "work_item",
            "target_id": item["id"],
            "new_due_at": "2026-09-01T00:00:00Z",
            "reason": "依赖方延期",
        },
        headers=admin_headers,
    )
    assert ddl.status_code == 403

    # 审核交付物 → 403
    reviewed = await client.post(
        f"/api/v1/work-items/{item['id']}/reviews",
        json={"deliverable_id": str(uuid.uuid4()), "decision": "approve"},
        headers=admin_headers,
    )
    assert reviewed.status_code == 403

    # 发起协作（admin 不是主执行人）→ 403
    collab = await client.post(
        f"/api/v1/work-items/{item['id']}/collaboration-requests",
        json={"assignee_id": str(bob.id), "title": "协作", "goal": "目标"},
        headers=admin_headers,
    )
    assert collab.status_code == 403

    # 提交交付物（admin 不是主执行人）→ 403
    deliverable = await client.post(
        f"/api/v1/work-items/{item['id']}/deliverables",
        json={"type": "text", "content": "越权交付"},
        headers=admin_headers,
    )
    assert deliverable.status_code == 403

    # Agent 建议反馈（leader 专属路由）→ 403
    feedback = await client.post(
        f"/api/v1/agent-suggestions/{uuid.uuid4()}/feedback",
        json={"action": "accepted"},
        headers=admin_headers,
    )
    assert feedback.status_code == 403

    # 普通成员管理操作仍 403（leader 语义不变的对照）
    member_create = await client.post(
        "/api/v1/members",
        json={"username": "dave", "password": "Dave123!", "display_name": "戴夫"},
        headers=ctx["alice_headers"],  # type: ignore[arg-type]
    )
    assert member_create.status_code == 403


# ---------- 不可被指派：assignee 为 admin → 422 ----------


async def test_admin_cannot_be_assigned(
    client: httpx.AsyncClient, project: Project
) -> None:
    """创建工作项/编辑/协作者/转派/协作的目标为 admin 成员时 → 422。"""
    ctx = await _make_ctx(client, project)
    admin: ProjectMember = ctx["admin"]  # type: ignore[assignment]
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    leader_headers = ctx["leader_headers"]
    alice_headers = ctx["alice_headers"]
    assert isinstance(leader_headers, dict) and isinstance(alice_headers, dict)

    # 创建工作项：主执行人 = admin → 422
    created = await client.post(
        "/api/v1/work-items",
        json={"title": "指派管理员", "priority": "low", "assignee_id": str(admin.id)},
        headers=leader_headers,
    )
    assert created.status_code == 422, created.text
    assert "管理员不参与工作协作" in created.json()["message"]

    # 创建工作项：协作者含 admin → 422
    with_collab = await client.post(
        "/api/v1/work-items",
        json={
            "title": "协作指派管理员",
            "priority": "low",
            "assignee_id": str(alice.id),
            "collaborator_ids": [str(admin.id)],
        },
        headers=leader_headers,
    )
    assert with_collab.status_code == 422, with_collab.text
    assert "管理员不参与工作协作" in with_collab.json()["message"]

    # 编辑工作项：改派主执行人为 admin → 422
    item = await _create_work_item(client, leader_headers, alice.id)
    patched = await client.patch(
        f"/api/v1/work-items/{item['id']}",
        json={"version": item["version"], "assignee_id": str(admin.id)},
        headers=leader_headers,
    )
    assert patched.status_code == 422, patched.text
    assert "管理员不参与工作协作" in patched.json()["message"]

    # 转派目标 = admin → 422
    transfer = await client.post(
        f"/api/v1/work-items/{item['id']}/transfer-requests",
        json={"to_member_id": str(admin.id), "reason": "r", "impact_note": "i"},
        headers=alice_headers,
    )
    assert transfer.status_code == 422, transfer.text
    assert "管理员不参与工作协作" in transfer.json()["message"]

    # 协作接收人 = admin → 422
    collab = await client.post(
        f"/api/v1/work-items/{item['id']}/collaboration-requests",
        json={"assignee_id": str(admin.id), "title": "协作", "goal": "目标"},
        headers=alice_headers,
    )
    assert collab.status_code == 422, collab.text
    assert "管理员不参与工作协作" in collab.json()["message"]
