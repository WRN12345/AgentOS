"""Ticket 05 验收：AI 辅助与后台扫描路径项目化（跨项目隔离测试）。

对照 issue 05 的 5 条 check：
- agent 运行带项目归属；其建议经运行推导归属（agent_suggestions 不冗余 project_id）；
- agent 全部工具查询限定当前项目，不泄漏其他项目上下文；
- agent 任务队列载荷显式携带项目上下文（worker 无请求头，不能靠 header 推导）；
- 到期扫描生成的通知按工作项项目落库；
- 风险扫描的项目级分析带项目维度、去重键项目化，不跨项目互相 skip。
"""

import uuid
from datetime import UTC, datetime, timedelta

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


# ---------- check ③：队列载荷显式携带项目上下文 ----------


async def test_agent_run_queue_payload_and_record_carry_project_id(
    project_a: Project, leader: ProjectMember
) -> None:
    """agent_runs.project_id 落库 + 队列载荷显式带 project_id（worker 无请求头）。"""
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
    """人工重试的队列载荷同样带 project_id（agent_runs 持久化的原归属）。"""
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
            run.status = "failed"  # 仅 failed 可重试（路由层校验，这里直接构造）
            from app.agents.service import retry_agent_run

            await retry_agent_run(session, redis_client, run)

        task = await dequeue(redis_client, timeout=2)
        assert task is not None
        assert task["payload"]["project_id"] == str(project_a.id)
    finally:
        await redis_client.aclose()


# ---------- check ②：工具查询限定当前项目，不泄漏 ----------


async def test_tools_list_are_project_scoped(project_a: Project, project_b: Project) -> None:
    """列表类工具按 project 过滤：A 项目只看到 A 的工作项/成员，看不到 B。"""
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
    """无 project 冗余列的表（transfer_requests）经 join 推导过滤，不跨项目泄漏。"""
    _, alice = await add_member(project_a, "alice_a2", "AliceA2!", display_name="爱丽丝")
    _, charlie = await add_member(project_a, "char_a", "CharA123!", display_name="查理")
    _, bob = await add_member(project_b, "bob_b2", "BobB2!", display_name="鲍勃")
    item_a = await _make_work_item(alice.id, project_id=project_a.id, status="READY")
    item_b = await _make_work_item(bob.id, project_id=project_b.id, status="READY")

    async with async_session_factory() as session:
        # A 项目一条待批转派；B 项目一条待批转派
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
                    to_member_id=alice.id,  # to_member 不必同项目，归属按 work_item 推导
                    reason="转给爱丽丝",
                    impact_note="影响可控",
                ),
            ]
        )
        await session.commit()

        # A 项目视角只看到 A 的转派
        rows_a = await TOOL_REGISTRY["list_transfer_history"].func(
            session, project_id=project_a.id
        )
        assert [r["work_item_id"] for r in rows_a] == [str(item_a.id)]

        # 待审批汇总同样按项目过滤
        pending_a = await TOOL_REGISTRY["list_pending_approvals"].func(
            session, project_id=project_a.id
        )
        assert [t["work_item_id"] for t in pending_a["pending_transfers"]] == [str(item_a.id)]


async def test_tools_single_item_ownership_blocks_cross_project(
    project_a: Project, project_b: Project
) -> None:
    """单条工具做项目归属校验：A 项目查 B 的工作项/文档视为不存在。"""
    _, alice = await add_member(project_a, "alice_a3", "AliceA3!", display_name="爱丽丝")
    _, bob = await add_member(project_b, "bob_b3", "BobB3!", display_name="鲍勃")
    item_a = await _make_work_item(alice.id, project_id=project_a.id, status="IN_PROGRESS")
    item_b = await _make_work_item(bob.id, project_id=project_b.id, status="IN_PROGRESS")

    async with async_session_factory() as session:
        # A 项目查自己 → 可见；查 B → None
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
        # 交付物元数据同理（deliverables.project_id 过滤）
        assert (
            await TOOL_REGISTRY["list_deliverable_metadata"].func(
                session, item_b.id, project_id=project_a.id
            )
        ) == []
        # dev_doc 经 work_items 推导归属
        assert (
            await TOOL_REGISTRY["get_dev_doc"].func(
                session, item_b.id, project_id=project_a.id
            )
        ) is None


# ---------- check ①：API 侧运行/建议跨项目隔离 ----------


async def test_agent_api_is_project_scoped(
    client, project_a: Project, project_b: Project, leader: ProjectMember
) -> None:
    """B 项目负责人看不到/操作不了 A 项目的 run 与建议（404/列表不包含）。"""
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
        # 执行图：生成 A 项目的建议
        task = await dequeue(redis_client, timeout=2)
        assert task is not None
        await handle_task(task, redis_client)
    finally:
        await redis_client.aclose()

    headers_b = await auth_headers(client, "lead_b", "LeadB123!", project_id=str(project_b.id))
    # 列表：B 看不到 A 的 run
    resp = await client.get("/api/v1/agent-runs", headers=headers_b)
    assert resp.status_code == 200
    assert all(r["id"] != str(run_a.id) for r in resp.json())
    # 单条：跨项目 → 404
    resp = await client.get(f"/api/v1/agent-runs/{run_a.id}", headers=headers_b)
    assert resp.status_code == 404
    # 重试：跨项目 → 404
    resp = await client.post(f"/api/v1/agent-runs/{run_a.id}/retry", headers=headers_b)
    assert resp.status_code == 404
    # 建议列表：B 看不到 A 的建议（经 run 推导归属）
    resp = await client.get("/api/v1/agent-suggestions", headers=headers_b)
    assert resp.status_code == 200
    assert all(s["run_id"] != str(run_a.id) for s in resp.json())

    # 对照：A 负责人能看到自己的 run（接口逻辑本身不变）
    headers_a = await auth_headers(client, "leader", "Leader123!", project_id=str(project_a.id))
    resp = await client.get(f"/api/v1/agent-runs/{run_a.id}", headers=headers_a)
    assert resp.status_code == 200


# ---------- check ④：到期扫描通知按工作项项目落库 ----------


async def test_due_scan_notifications_store_correct_project(
    project_a: Project, project_b: Project, leader: ProjectMember
) -> None:
    """due_scan 生成的提醒通知按各自工作项项目落库（notifications.project_id）。"""
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


# ---------- check ⑤：风险扫描按项目维度去重，不跨项目互相 skip ----------


async def test_risk_scan_dedup_is_per_project(project_a: Project, project_b: Project) -> None:
    """A 项目已有活跃 workflow_risk run → 只跳过 A，仍为 B 投递。"""
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
    """两个项目都有活跃 run → 各自跳过，不投递（且不互相 skip 反方向）。"""
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


# ---------- 评审 #5：通知读隔离（同一用户挂双项目） ----------


async def test_notification_list_and_read_isolated_by_project(
    client, project_a: Project, project_b: Project
) -> None:
    """同一全局账号挂 A/B：A 上下文只见 A 通知；B 上下文读 A 通知 → 404。"""
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

    # A 上下文：只见 A 通知
    resp = await client.get("/api/v1/notifications", headers=headers_a)
    assert resp.status_code == 200
    assert [n["id"] for n in resp.json()["items"]] == [str(noti_a.id)]

    # B 上下文：只见 B 通知
    resp = await client.get("/api/v1/notifications", headers=headers_b)
    assert resp.status_code == 200
    assert [n["id"] for n in resp.json()["items"]] == [str(noti_b.id)]

    # B 上下文读 A 通知 → 404（不暴露存在性）
    resp = await client.post(f"/api/v1/notifications/{noti_a.id}/read", headers=headers_b)
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"

    # 对照：A 上下文读自己的 → 200
    resp = await client.post(f"/api/v1/notifications/{noti_a.id}/read", headers=headers_a)
    assert resp.status_code == 200
    assert resp.json()["is_read"] is True
