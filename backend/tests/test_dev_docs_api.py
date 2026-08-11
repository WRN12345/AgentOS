"""开发文档前置（先文档后开发）接口集成测试（设计文档 2026-07-30 §3/§7）。

覆盖：
- CRUD 与状态机：GET 404 → PUT 创建草稿 → 编辑 → submit（doc_version+1，
  触发 dev_doc_review event run）→ SUBMITTED 只读 → confirm/return；
- 权限：PUT/submit 仅主执行人，confirm/return/waive 仅负责人，GET 透明；
- 开工拦截：无 CONFIRMED 文档且未豁免时 start → 409 DEV_DOC_REQUIRED；
  确认通过或豁免后可开工；unblock 不重复校验；
- 审批聚合：SUBMITTED 出现在 GET /approvals（kind=dev_doc），
  CONFIRMED 进入 GET /approvals/processed；
- 审计：提交/确认/打回/豁免均写 audit_events。
"""

import httpx
from sqlalchemy import select

from app.agents.models import AgentRun
from app.domains.audit.models import AuditEvent
from app.domains.project.models import Project, ProjectMember
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member, auth_headers

LEADER_PW = "Leader123!"
ALICE_PW = "Alice123!"
BOB_PW = "Bob123!"


async def _setup(client: httpx.AsyncClient, project: Project) -> dict[str, object]:
    """leader（负责人）+ alice（主执行人）+ bob（普通成员）+ 已发布工作项（READY）。"""
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


async def _put_doc(client, headers, item_id: str, content: str, version: int | None = None):
    payload: dict[str, object] = {"content": content}
    if version is not None:
        payload["version"] = version
    return await client.put(f"/api/v1/work-items/{item_id}/dev-doc", json=payload, headers=headers)


async def _start(client, headers, item_id: str, version: int = 2):
    return await client.post(
        f"/api/v1/work-items/{item_id}/start", json={"version": version}, headers=headers
    )


# ---------- 开工拦截（§7 验收 1） ----------


async def test_start_blocked_until_dev_doc_confirmed(
    client: httpx.AsyncClient, project: Project
) -> None:
    """无文档 → start 409；草稿/已提交未确认 → 仍 409；确认通过后可开工。"""
    ctx = await _setup(client, project)
    item = ctx["item"]
    alice_headers = ctx["alice_headers"]
    leader_headers = ctx["leader_headers"]
    item_id = item["id"]  # type: ignore[index]

    resp = await _start(client, alice_headers, item_id)  # type: ignore[arg-type]
    assert resp.status_code == 409
    assert resp.json()["code"] == "DEV_DOC_REQUIRED"
    assert "开发文档" in resp.json()["message"]

    # 草稿状态不足以开工
    doc = await _put_doc(client, alice_headers, item_id, "# 方案\n用向量检索", )  # type: ignore[arg-type]
    assert doc.status_code == 200, doc.text
    resp = await _start(client, alice_headers, item_id)  # type: ignore[arg-type]
    assert resp.status_code == 409

    # 已提交未确认仍拦截
    submitted = await client.post(
        f"/api/v1/work-items/{item_id}/dev-doc/submit",
        json={"version": doc.json()["version"]},
        headers=alice_headers,  # type: ignore[arg-type]
    )
    assert submitted.status_code == 200, submitted.text
    resp = await _start(client, alice_headers, item_id)  # type: ignore[arg-type]
    assert resp.status_code == 409

    # 负责人确认后放行
    confirmed = await client.post(
        f"/api/v1/work-items/{item_id}/dev-doc/confirm",
        json={"version": submitted.json()["version"]},
        headers=leader_headers,  # type: ignore[arg-type]
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "CONFIRMED"
    assert confirmed.json()["confirmed_by"]["display_name"] == "负责人"
    assert confirmed.json()["confirmed_at"] is not None
    resp = await _start(client, alice_headers, item_id)  # type: ignore[arg-type]
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "IN_PROGRESS"

    # unblock 不重复校验：block 后 unblock 直接放行
    blocked = await client.post(
        f"/api/v1/work-items/{item_id}/block", json={"version": 3}, headers=alice_headers  # type: ignore[arg-type]
    )
    assert blocked.status_code == 200, blocked.text
    unblocked = await client.post(
        f"/api/v1/work-items/{item_id}/unblock", json={"version": 4}, headers=alice_headers  # type: ignore[arg-type]
    )
    assert unblocked.status_code == 200, unblocked.text


async def test_waive_allows_start_without_doc(
    client: httpx.AsyncClient, project: Project
) -> None:
    """负责人豁免（无文档时直接 waive 创建占位行）→ 可开工；豁免写审计。"""
    ctx = await _setup(client, project)
    item = ctx["item"]
    item_id = item["id"]  # type: ignore[index]

    waived = await client.post(
        f"/api/v1/work-items/{item_id}/dev-doc/waive",
        json={},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert waived.status_code == 200, waived.text
    assert waived.json()["waived"] is True
    assert waived.json()["status"] == "DRAFT"

    resp = await _start(client, ctx["alice_headers"], item_id)  # type: ignore[arg-type]
    assert resp.status_code == 200, resp.text

    async with async_session_factory() as session:
        event = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "dev_doc.waived")
            )
        ).scalar_one_or_none()
        assert event is not None


# ---------- CRUD 与状态机 ----------


async def test_dev_doc_crud_and_submit_triggers_agent_review(
    client: httpx.AsyncClient, project: Project
) -> None:
    """GET 404 → PUT 创建（DRAFT）→ 编辑（乐观锁）→ submit（doc_version+1 +
    触发 dev_doc_review event run）→ SUBMITTED 后 PUT 409 → 重交版本冲突 409。"""
    ctx = await _setup(client, project)
    item = ctx["item"]
    alice_headers = ctx["alice_headers"]
    item_id = item["id"]  # type: ignore[index]

    resp = await client.get(f"/api/v1/work-items/{item_id}/dev-doc", headers=alice_headers)  # type: ignore[arg-type]
    assert resp.status_code == 404

    doc = await _put_doc(client, alice_headers, item_id, "# 开发方案\n检索 + 生成")  # type: ignore[arg-type]
    assert doc.status_code == 200, doc.text
    body = doc.json()
    assert body["status"] == "DRAFT"
    assert body["doc_version"] == 0
    assert body["version"] == 1
    assert body["author"]["display_name"] == "爱丽丝"

    edited = await _put_doc(client, alice_headers, item_id, "# 开发方案 v2", version=1)  # type: ignore[arg-type]
    assert edited.status_code == 200, edited.text
    assert edited.json()["version"] == 2
    stale = await _put_doc(client, alice_headers, item_id, "旧版本写入", version=1)  # type: ignore[arg-type]
    assert stale.status_code == 409
    assert stale.json()["code"] == "DEV_DOC_VERSION_CONFLICT"

    submitted = await client.post(
        f"/api/v1/work-items/{item_id}/dev-doc/submit",
        json={"version": 2},
        headers=alice_headers,  # type: ignore[arg-type]
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "SUBMITTED"
    assert submitted.json()["doc_version"] == 1

    # 触发 Agent 初审（event 触发，建议性质，不阻塞）
    async with async_session_factory() as session:
        run = (
            await session.execute(
                select(AgentRun).where(
                    AgentRun.agent_type == "dev_doc_review",
                    AgentRun.trigger_source == "event",
                )
            )
        ).scalar_one_or_none()
        assert run is not None
        assert str(run.work_item_id) == item_id

    # SUBMITTED 只读：编辑与重复提交均 409
    locked = await _put_doc(client, alice_headers, item_id, "改动", version=3)  # type: ignore[arg-type]
    assert locked.status_code == 409
    assert locked.json()["code"] == "DEV_DOC_INVALID_TRANSITION"
    resubmitted = await client.post(
        f"/api/v1/work-items/{item_id}/dev-doc/submit",
        json={"version": 3},
        headers=alice_headers,  # type: ignore[arg-type]
    )
    assert resubmitted.status_code == 409
    assert resubmitted.json()["code"] == "DEV_DOC_INVALID_TRANSITION"


async def test_return_flow_allows_edit_and_resubmit(
    client: httpx.AsyncClient, project: Project
) -> None:
    """打回（附理由）→ RETURNED 可再编辑 → 重新提交 doc_version=2，理由清空。"""
    ctx = await _setup(client, project)
    item = ctx["item"]
    alice_headers = ctx["alice_headers"]
    leader_headers = ctx["leader_headers"]
    item_id = item["id"]  # type: ignore[index]

    doc = await _put_doc(client, alice_headers, item_id, "# 方案")  # type: ignore[arg-type]
    submitted = await client.post(
        f"/api/v1/work-items/{item_id}/dev-doc/submit",
        json={"version": doc.json()["version"]},
        headers=alice_headers,  # type: ignore[arg-type]
    )
    assert submitted.status_code == 200

    returned = await client.post(
        f"/api/v1/work-items/{item_id}/dev-doc/return",
        json={"version": submitted.json()["version"], "review_note": "缺少接口约定"},
        headers=leader_headers,  # type: ignore[arg-type]
    )
    assert returned.status_code == 200, returned.text
    assert returned.json()["status"] == "RETURNED"
    assert returned.json()["review_note"] == "缺少接口约定"

    edited = await _put_doc(
        client, alice_headers, item_id, "# 方案\n## 接口约定\nPOST /query",  # type: ignore[arg-type]
        version=returned.json()["version"],
    )
    assert edited.status_code == 200, edited.text
    resubmitted = await client.post(
        f"/api/v1/work-items/{item_id}/dev-doc/submit",
        json={"version": edited.json()["version"]},
        headers=alice_headers,  # type: ignore[arg-type]
    )
    assert resubmitted.status_code == 200, resubmitted.text
    assert resubmitted.json()["doc_version"] == 2
    assert resubmitted.json()["review_note"] is None

    # 审计留痕：提交/打回均有事件
    async with async_session_factory() as session:
        actions = set(
            (await session.execute(select(AuditEvent.action).where(AuditEvent.target_type == "dev_doc")))
            .scalars()
            .all()
        )
    assert {"dev_doc.created", "dev_doc.submitted", "dev_doc.returned"} <= actions


# ---------- 权限 ----------


async def test_dev_doc_permissions(client: httpx.AsyncClient, project: Project) -> None:
    """PUT/submit 仅主执行人；confirm/return/waive 仅负责人；GET 透明；匿名 401。"""
    ctx = await _setup(client, project)
    item = ctx["item"]
    alice_headers = ctx["alice_headers"]
    bob_headers = ctx["bob_headers"]
    item_id = item["id"]  # type: ignore[index]

    resp = await _put_doc(client, bob_headers, item_id, "越权写入")  # type: ignore[arg-type]
    assert resp.status_code == 403

    doc = await _put_doc(client, alice_headers, item_id, "# 方案")  # type: ignore[arg-type]
    assert doc.status_code == 200
    # GET 对任何成员透明
    resp = await client.get(f"/api/v1/work-items/{item_id}/dev-doc", headers=bob_headers)  # type: ignore[arg-type]
    assert resp.status_code == 200
    assert resp.json()["content"] == "# 方案"

    submitted = await client.post(
        f"/api/v1/work-items/{item_id}/dev-doc/submit",
        json={"version": doc.json()["version"]},
        headers=alice_headers,  # type: ignore[arg-type]
    )
    assert submitted.status_code == 200
    # 非负责人不能确认/打回/豁免
    for path, payload in (
        ("confirm", {"version": submitted.json()["version"]}),
        ("return", {"version": submitted.json()["version"], "review_note": "理由"}),
        ("waive", {"version": submitted.json()["version"]}),
    ):
        resp = await client.post(
            f"/api/v1/work-items/{item_id}/dev-doc/{path}", json=payload, headers=alice_headers  # type: ignore[arg-type]
        )
        assert resp.status_code == 403, path

    resp = await client.get(f"/api/v1/work-items/{item_id}/dev-doc")
    assert resp.status_code == 401


# ---------- 审批中心聚合（§4.5） ----------


async def test_dev_doc_in_approvals_pending_and_processed(
    client: httpx.AsyncClient, project: Project
) -> None:
    """SUBMITTED → pending 聚合（kind=dev_doc，"第 N 次提交"）；
    confirm 后离开 pending、进入 processed（含 approved_by/approved_at）。"""
    ctx = await _setup(client, project)
    leader: ProjectMember = ctx["leader"]  # type: ignore[assignment]
    item = ctx["item"]
    alice_headers = ctx["alice_headers"]
    leader_headers = ctx["leader_headers"]
    item_id = item["id"]  # type: ignore[index]

    doc = await _put_doc(client, alice_headers, item_id, "# 方案")  # type: ignore[arg-type]
    submitted = await client.post(
        f"/api/v1/work-items/{item_id}/dev-doc/submit",
        json={"version": doc.json()["version"]},
        headers=alice_headers,  # type: ignore[arg-type]
    )
    assert submitted.status_code == 200

    resp = await client.get("/api/v1/approvals", headers=leader_headers)  # type: ignore[arg-type]
    assert resp.status_code == 200
    kinds = [a["kind"] for a in resp.json()]
    assert kinds == ["dev_doc"]
    entry = resp.json()[0]
    assert entry["status"] == "SUBMITTED"
    assert entry["work_item_title"] == "RAG 工作项"
    assert entry["summary"] == "RAG 工作项：第 1 次提交"
    assert entry["requested_by"]["display_name"] == "爱丽丝"
    assert entry["doc_version"] == 1

    confirmed = await client.post(
        f"/api/v1/work-items/{item_id}/dev-doc/confirm",
        json={"version": submitted.json()["version"]},
        headers=leader_headers,  # type: ignore[arg-type]
    )
    assert confirmed.status_code == 200

    resp = await client.get("/api/v1/approvals", headers=leader_headers)  # type: ignore[arg-type]
    assert resp.json() == []
    resp = await client.get("/api/v1/approvals/processed", headers=leader_headers)  # type: ignore[arg-type]
    assert resp.status_code == 200
    entries = resp.json()
    assert [a["kind"] for a in entries] == ["dev_doc"]
    assert entries[0]["status"] == "CONFIRMED"
    assert entries[0]["approved_by"] == {"id": str(leader.id), "display_name": "负责人"}
    assert entries[0]["approved_at"] is not None
    assert entries[0]["doc_version"] == 1
