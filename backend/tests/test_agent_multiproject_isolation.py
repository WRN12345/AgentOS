"""验证 AI 辅助、通知和后台扫描的跨项目隔离。

运行、队列载荷和通知必须携带项目归属；工具查询只能访问当前项目。风险扫描应按
项目独立去重，并在并发扫描和陈旧租约场景下保持每个项目最多一个活跃运行。
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agents.models import AgentRun, AgentSuggestion
from app.agents.service import request_agent_analysis
from app.agents.tools import TOOL_REGISTRY
from app.domains.identity.models import User
from app.domains.notifications.models import Notification
from app.domains.notifications.service import notify
from app.domains.project.models import Project, ProjectMember
from app.domains.transfers.models import TransferRequest
from app.domains.work_items.models import WorkItem
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.queue.queue import QUEUE_KEY, dequeue
from app.workers.due_scan import scan_due_reminders
from app.workers.risk_scan import run_risk_scan
from app.workers.worker import handle_task
from tests.conftest import add_member, add_member_for_existing_user, auth_headers


async def _make_work_item(
    assignee_id: uuid.UUID,
    *,
    project_id: uuid.UUID,
    status: str = "READY",
    due_at: datetime | None = None,
) -> WorkItem:
    async with async_session_factory() as session:
        item = WorkItem(
            title="跨项目隔离测试工作项",
            description="描述",
            project_id=project_id,
            assignee_id=assignee_id,
            status=status,
            due_at=due_at,
        )
        item.collaborators = []
        session.add(item)
        await session.commit()
    return item


async def test_event_triggered_runs_receive_work_item_project_id(monkeypatch) -> None:
    """工作项和开发文档事件触发的运行应继承工作项项目归属。"""
    from app.domains.dev_docs import service as dev_doc_service
    from app.domains.work_items import service as work_item_service

    captured: list[uuid.UUID | None] = []

    class FakeRedis:
        async def aclose(self) -> None:
            return None

    async def fake_request(*_args, **kwargs):
        captured.append(kwargs.get("project_id"))
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(work_item_service, "create_redis_client", lambda: FakeRedis())
    monkeypatch.setattr(dev_doc_service, "create_redis_client", lambda: FakeRedis())
    monkeypatch.setattr(work_item_service, "request_agent_analysis", fake_request)
    monkeypatch.setattr(dev_doc_service, "request_agent_analysis", fake_request)

    project_id = uuid.uuid4()
    item = SimpleNamespace(id=uuid.uuid4(), project_id=project_id)
    await work_item_service._dispatch_deliverable_review(object(), item)
    await dev_doc_service._dispatch_dev_doc_review(object(), item)

    assert captured == [project_id, project_id]


async def test_agent_run_queue_payload_and_record_carry_project_id(
    project_a: Project, leader: ProjectMember
) -> None:
    """运行记录和队列载荷都应显式保存项目 ID。"""
    item = await _make_work_item(leader.id, project_id=project_a.id)
    redis_client = create_redis_client()
    try:
        await redis_client.delete(QUEUE_KEY)
        async with async_session_factory() as session:
            run = await request_agent_analysis(
                session,
                redis_client,
                agent_type="echo",
                project_id=project_a.id,
                trigger_source="manual",
                work_item_id=item.id,
            )
        assert run.project_id == project_a.id

        task = await dequeue(redis_client, timeout=2)
        assert task is not None
        assert task["type"] == "agent.run"
        assert task["payload"]["project_id"] == str(project_a.id)
        assert task["payload"]["run_id"] == str(run.id)
    finally:
        await redis_client.aclose()


async def test_retry_payload_carries_project_id(
    project_a: Project, leader: ProjectMember
) -> None:
    """人工重试应从运行记录恢复原项目 ID 到队列载荷。"""
    item = await _make_work_item(leader.id, project_id=project_a.id)
    redis_client = create_redis_client()
    try:
        await redis_client.delete(QUEUE_KEY)
        async with async_session_factory() as session:
            run = await request_agent_analysis(
                session,
                redis_client,
                agent_type="echo",
                project_id=project_a.id,
                work_item_id=item.id,
            )
            run.status = "failed"  # 服务只允许重试失败运行。
            from app.agents.service import retry_agent_run

            await retry_agent_run(session, redis_client, run)

        task = await dequeue(redis_client, timeout=2)
        assert task is not None
        assert task["payload"]["project_id"] == str(project_a.id)
    finally:
        await redis_client.aclose()


async def test_tools_list_are_project_scoped(project_a: Project, project_b: Project) -> None:
    """列表工具只能返回当前项目的工作项和成员。"""
    _, alice = await add_member(project_a, "alice_a", "AliceA123!", display_name="爱丽丝")
    _, bob = await add_member(project_b, "bob_b", "BobB123!", display_name="鲍勃")
    item_a = await _make_work_item(alice.id, project_id=project_a.id, status="IN_PROGRESS")
    await _make_work_item(bob.id, project_id=project_b.id, status="IN_PROGRESS")

    async with async_session_factory() as session:
        open_a = await TOOL_REGISTRY["list_open_work_items"].func(
            session, project_id=project_a.id
        )
        assert [i["id"] for i in open_a] == [str(item_a.id)]

        caps_a = await TOOL_REGISTRY["list_member_capabilities"].func(
            session, project_id=project_a.id
        )
        assert all(c["member_id"] == str(alice.id) for c in caps_a)

        assignable_a = await TOOL_REGISTRY["list_assignable_members"].func(
            session, project_id=project_a.id
        )
        assert {m["member_id"] for m in assignable_a} == {str(alice.id)}


async def test_tools_join_derived_are_project_scoped(
    project_a: Project, project_b: Project
) -> None:
    """没有项目列的实体应通过关联关系推导归属并隔离查询。"""
    _, alice = await add_member(project_a, "alice_a2", "AliceA2!", display_name="爱丽丝")
    _, charlie = await add_member(project_a, "char_a", "CharA123!", display_name="查理")
    _, bob = await add_member(project_b, "bob_b2", "BobB2!", display_name="鲍勃")
    item_a = await _make_work_item(alice.id, project_id=project_a.id, status="READY")
    item_b = await _make_work_item(bob.id, project_id=project_b.id, status="READY")

    async with async_session_factory() as session:
        # 分别创建两个项目的数据，验证关联查询不会混合结果。
        session.add_all(
            [
                TransferRequest(
                    work_item_id=item_a.id,
                    from_member_id=alice.id,
                    to_member_id=charlie.id,
                    reason="转给查理",
                    impact_note="影响可控",
                ),
                TransferRequest(
                    work_item_id=item_b.id,
                    from_member_id=bob.id,
                    to_member_id=alice.id,  # 转派归属由工作项决定，而非目标成员。
                    reason="转给爱丽丝",
                    impact_note="影响可控",
                ),
            ]
        )
        await session.commit()

        rows_a = await TOOL_REGISTRY["list_transfer_history"].func(
            session, project_id=project_a.id
        )
        assert [r["work_item_id"] for r in rows_a] == [str(item_a.id)]

        pending_a = await TOOL_REGISTRY["list_pending_approvals"].func(
            session, project_id=project_a.id
        )
        assert [t["work_item_id"] for t in pending_a["pending_transfers"]] == [str(item_a.id)]


async def test_tools_single_item_ownership_blocks_cross_project(
    project_a: Project, project_b: Project
) -> None:
    """单条查询访问其他项目实体时应表现为不存在。"""
    _, alice = await add_member(project_a, "alice_a3", "AliceA3!", display_name="爱丽丝")
    _, bob = await add_member(project_b, "bob_b3", "BobB3!", display_name="鲍勃")
    item_a = await _make_work_item(alice.id, project_id=project_a.id, status="IN_PROGRESS")
    item_b = await _make_work_item(bob.id, project_id=project_b.id, status="IN_PROGRESS")

    async with async_session_factory() as session:
        # 跨项目查询返回空值，避免泄漏实体存在性。
        assert (
            await TOOL_REGISTRY["get_work_item_overview"].func(
                session, item_a.id, project_id=project_a.id
            )
        ) is not None
        assert (
            await TOOL_REGISTRY["get_work_item_overview"].func(
                session, item_b.id, project_id=project_a.id
            )
        ) is None
        assert (
            await TOOL_REGISTRY["list_deliverable_metadata"].func(
                session, item_b.id, project_id=project_a.id
            )
        ) == []
        assert (
            await TOOL_REGISTRY["get_dev_doc"].func(
                session, item_b.id, project_id=project_a.id
            )
        ) is None


async def test_agent_api_is_project_scoped(
    client, project_a: Project, project_b: Project, leader: ProjectMember
) -> None:
    """其他项目负责人不得查看或操作当前项目的运行和建议。"""
    _, leader_b = await add_member(
        project_b, "lead_b", "LeadB123!", role="leader", display_name="负责人B"
    )
    item_a = await _make_work_item(leader.id, project_id=project_a.id)
    redis_client = create_redis_client()
    try:
        await redis_client.delete(QUEUE_KEY)
        async with async_session_factory() as session:
            run_a = await request_agent_analysis(
                session,
                redis_client,
                agent_type="echo",
                project_id=project_a.id,
                work_item_id=item_a.id,
                prompt="A 项目需求",
            )
        task = await dequeue(redis_client, timeout=2)
        assert task is not None
        await handle_task(task, redis_client)
    finally:
        await redis_client.aclose()

    headers_b = await auth_headers(client, "lead_b", "LeadB123!", project_id=str(project_b.id))
    resp = await client.get("/api/v1/agent-runs", headers=headers_b)
    assert resp.status_code == 200
    assert all(r["id"] != str(run_a.id) for r in resp.json())
    resp = await client.get(f"/api/v1/agent-runs/{run_a.id}", headers=headers_b)
    assert resp.status_code == 404
    resp = await client.post(f"/api/v1/agent-runs/{run_a.id}/retry", headers=headers_b)
    assert resp.status_code == 404
    # 建议通过关联运行推导项目归属，也必须保持隔离。
    resp = await client.get("/api/v1/agent-suggestions", headers=headers_b)
    assert resp.status_code == 200
    assert all(s["run_id"] != str(run_a.id) for s in resp.json())

    headers_a = await auth_headers(client, "leader", "Leader123!", project_id=str(project_a.id))
    resp = await client.get(f"/api/v1/agent-runs/{run_a.id}", headers=headers_a)
    assert resp.status_code == 200


async def test_agent_trigger_rejects_cross_project_work_item(
    client, project_a: Project, project_b: Project, leader: ProjectMember
) -> None:
    """跨项目引用工作项时应返回未找到，且不得创建运行记录。"""
    _, leader_b = await add_member(
        project_b, "lead_cross", "LeadCross1!", role="leader", display_name="负责人B"
    )
    item_a = await _make_work_item(leader.id, project_id=project_a.id)
    headers_b = await auth_headers(
        client, "lead_cross", "LeadCross1!", project_id=str(project_b.id)
    )

    before = None
    async with async_session_factory() as session:
        before = len((await session.execute(select(AgentRun))).scalars().all())

    response = await client.post(
        f"/api/v1/work-items/{item_a.id}/agent-analysis",
        json={"agent_type": "echo"},
        headers=headers_b,
    )
    assert response.status_code == 404

    response = await client.post(
        "/api/v1/agent-analysis",
        json={"agent_type": "workflow_risk", "work_item_id": str(item_a.id)},
        headers=headers_b,
    )
    assert response.status_code == 404
    async with async_session_factory() as session:
        after = len((await session.execute(select(AgentRun))).scalars().all())
    assert after == before


async def test_due_scan_notifications_store_correct_project(
    project_a: Project, project_b: Project, leader: ProjectMember
) -> None:
    """到期扫描生成的通知应归属于对应工作项项目。"""
    due = datetime.now(UTC) + timedelta(hours=1)
    item_a = await _make_work_item(leader.id, project_id=project_a.id, due_at=due)
    _, leader_b = await add_member(
        project_b, "lead_due", "LeadDue1!", role="leader", display_name="负责人B"
    )
    item_b = await _make_work_item(leader_b.id, project_id=project_b.id, due_at=due)

    redis_client = create_redis_client()
    try:
        stats = await scan_due_reminders(redis_client)
        assert stats["sent"] == 2

        async with async_session_factory() as session:
            notices = list((await session.execute(select(Notification))).scalars().all())
        by_link = {n.link: n for n in notices}
        assert by_link[f"/work-items/{item_a.id}"].project_id == project_a.id
        assert by_link[f"/work-items/{item_b.id}"].project_id == project_b.id
    finally:
        await redis_client.aclose()


async def test_risk_scan_dedup_is_per_project(project_a: Project, project_b: Project) -> None:
    """一个项目的活跃风险运行不得阻止其他项目投递。"""
    redis_client = create_redis_client()
    try:
        async with async_session_factory() as session:
            await request_agent_analysis(
                session,
                redis_client,
                agent_type="workflow_risk",
                project_id=project_a.id,
                trigger_source="scheduler",
            )

        result = await run_risk_scan(redis_client)

        assert set(result["skipped"]) == {str(project_a.id)}
        enqueued_projects = [e["project_id"] for e in result["enqueued"]]
        assert enqueued_projects == [str(project_b.id)]
    finally:
        await redis_client.aclose()


async def test_risk_scan_skips_only_projects_with_active_runs(
    project_a: Project, project_b: Project
) -> None:
    """每个项目都应独立跳过自己的活跃风险运行。"""
    redis_client = create_redis_client()
    try:
        async with async_session_factory() as session:
            await request_agent_analysis(
                session,
                redis_client,
                agent_type="workflow_risk",
                project_id=project_a.id,
                trigger_source="scheduler",
            )
            await request_agent_analysis(
                session,
                redis_client,
                agent_type="workflow_risk",
                project_id=project_b.id,
                trigger_source="scheduler",
            )

        result = await run_risk_scan(redis_client)

        assert set(result["skipped"]) == {str(project_a.id), str(project_b.id)}
        assert result["enqueued"] == []
    finally:
        await redis_client.aclose()


async def test_concurrent_risk_scans_create_one_run_per_project(project_a: Project) -> None:
    """并发扫描应通过 advisory lock 保证每个项目只创建一个运行。"""
    redis_client = create_redis_client()
    try:
        await redis_client.delete(QUEUE_KEY)
        await asyncio.gather(run_risk_scan(redis_client), run_risk_scan(redis_client))
        async with async_session_factory() as session:
            runs = (
                await session.execute(
                    select(AgentRun).where(
                        AgentRun.project_id == project_a.id,
                        AgentRun.agent_type == "workflow_risk",
                    )
                )
            ).scalars().all()
        assert len(runs) == 1
    finally:
        await redis_client.aclose()


async def test_stale_pending_risk_run_does_not_block_forever(project_a: Project) -> None:
    """超过租约的待处理运行应失败，并允许本轮扫描创建替代运行。"""
    redis_client = create_redis_client()
    try:
        async with async_session_factory() as session:
            stale = await request_agent_analysis(
                session,
                redis_client,
                agent_type="workflow_risk",
                project_id=project_a.id,
                trigger_source="scheduler",
            )
            stale.updated_at = datetime.now(UTC) - timedelta(hours=1)
            await session.commit()

        result = await run_risk_scan(redis_client)
        assert [entry["project_id"] for entry in result["enqueued"]] == [str(project_a.id)]
        async with async_session_factory() as session:
            runs = (
                await session.execute(
                    select(AgentRun)
                    .where(
                        AgentRun.project_id == project_a.id,
                        AgentRun.agent_type == "workflow_risk",
                    )
                    .order_by(AgentRun.created_at)
                )
            ).scalars().all()
        assert [run.status for run in runs] == ["failed", "pending"]
        assert "lease expired" in (runs[0].error or "").lower()
    finally:
        await redis_client.aclose()


async def test_notification_list_and_read_isolated_by_project(
    client, project_a: Project, project_b: Project
) -> None:
    """同一账号加入多个项目时，通知列表和已读操作仍应按项目隔离。"""
    _, member_a = await add_member(project_a, "noti_user", "Noti123!", display_name="通知人")
    async with async_session_factory() as session:
        user = await session.get(User, member_a.user_id)
    member_b = await add_member_for_existing_user(
        async_session_factory, project_b, user, display_name="通知人-B"
    )

    async with async_session_factory() as session:
        noti_a = await notify(
            session,
            project_id=project_a.id,
            recipient_id=member_a.id,
            type="test.event",
            title="A 通知",
            body="A 正文",
        )
        noti_b = await notify(
            session,
            project_id=project_b.id,
            recipient_id=member_b.id,
            type="test.event",
            title="B 通知",
            body="B 正文",
        )
        await session.commit()

    headers_a = await auth_headers(client, "noti_user", "Noti123!", project_id=str(project_a.id))
    headers_b = await auth_headers(client, "noti_user", "Noti123!", project_id=str(project_b.id))

    resp = await client.get("/api/v1/notifications", headers=headers_a)
    assert resp.status_code == 200
    assert [n["id"] for n in resp.json()["items"]] == [str(noti_a.id)]

    resp = await client.get("/api/v1/notifications", headers=headers_b)
    assert resp.status_code == 200
    assert [n["id"] for n in resp.json()["items"]] == [str(noti_b.id)]

    # 跨项目已读操作返回未找到，避免暴露通知存在性。
    resp = await client.post(f"/api/v1/notifications/{noti_a.id}/read", headers=headers_b)
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"

    resp = await client.post(f"/api/v1/notifications/{noti_a.id}/read", headers=headers_a)
    assert resp.status_code == 200
    assert resp.json()["is_read"] is True
