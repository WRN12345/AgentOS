"""验证 Agent 失败恢复、指数退避、worker 韧性和人工重试。

暂时性模型错误应经延迟队列重试并在耗尽上限后失败，确定性校验错误不得重试。
Agent 故障不能拖垮 worker 或核心业务；人工重试必须复用原运行且避免重复建议。
"""

import json
import time
import uuid

import httpx
import pytest
from sqlalchemy import func, select

from app.agents.graphs import base as graph_base
from app.agents.models import AgentRun, AgentSuggestion
from app.agents.service import request_agent_analysis
from app.core.config import settings
from app.domains.project.models import Project
from app.domains.work_items.models import WorkItem
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.models.errors import ModelTimeoutError, ModelUnavailableError
from app.infrastructure.queue.queue import (
    DELAYED_QUEUE_KEY,
    QUEUE_KEY,
    dequeue,
    enqueue_delayed,
    promote_due_delayed,
)
from app.workers import worker as worker_module
from app.workers.agent_run import retry_delay_seconds
from app.workers.worker import handle_task, safe_handle_task
from tests.conftest import add_member, auth_headers

LEADER_PW = "Leader123!"
ALICE_PW = "Alice123!"
BOB_PW = "Bob123!"


async def _clean_queues(redis_client) -> None:  # noqa: ANN001
    """清空即时和延迟队列，隔离其他用例遗留的任务。"""
    await redis_client.delete(QUEUE_KEY, DELAYED_QUEUE_KEY)


def _run_task(run_id: uuid.UUID, prompt: str = "") -> dict:
    return {
        "id": str(uuid.uuid4()),
        "type": "agent.run",
        "payload": {"run_id": str(run_id), "agent_type": "echo", "prompt": prompt},
    }


async def _get_run(run_id: uuid.UUID) -> AgentRun:
    async with async_session_factory() as session:
        run = await session.get(AgentRun, run_id)
        assert run is not None
        return run


def test_retry_delay_is_exponential(monkeypatch: pytest.MonkeyPatch) -> None:
    """重试间隔应按 ``base * 2^attempt`` 指数增长。"""
    monkeypatch.setattr(settings, "agent_run_retry_base_seconds", 30.0)
    assert [retry_delay_seconds(i) for i in range(3)] == [30.0, 60.0, 120.0]
    monkeypatch.setattr(settings, "agent_run_retry_base_seconds", 5.0)
    assert [retry_delay_seconds(i) for i in range(4)] == [5.0, 10.0, 20.0, 40.0]


async def test_delayed_queue_promotes_due_tasks() -> None:
    """延迟任务到期前不得搬移，到期后应进入即时队列供消费。"""
    redis_client = create_redis_client()
    try:
        await _clean_queues(redis_client)
        before = time.time()
        task = await enqueue_delayed(redis_client, "example.ping", {"source": "test"}, delay_seconds=60)
        entries = await redis_client.zrange(DELAYED_QUEUE_KEY, 0, -1, withscores=True)
        assert len(entries) == 1
        score = entries[0][1]
        assert before + 55 < score <= before + 60.5

        # 截止时间是延迟队列与即时队列之间的唯一搬移条件。
        assert await promote_due_delayed(redis_client, now=before + 59) == 0
        assert await redis_client.llen(QUEUE_KEY) == 0
        assert await promote_due_delayed(redis_client, now=before + 61) == 1
        assert await redis_client.zcard(DELAYED_QUEUE_KEY) == 0
        dequeued = await dequeue(redis_client, timeout=2)
        assert dequeued is not None
        assert dequeued["id"] == task["id"]
    finally:
        await redis_client.aclose()


async def test_model_timeout_retries_with_backoff_then_fails(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """持续超时应按指数间隔重试，并在耗尽上限后进入失败终态。"""
    monkeypatch.setattr(settings, "agent_run_max_retries", 3)
    monkeypatch.setattr(settings, "agent_run_retry_base_seconds", 30.0)

    def _boom(state):  # noqa: ANN001, ANN202
        raise ModelTimeoutError("ollama read timeout")

    monkeypatch.setitem(graph_base.CAPABILITIES, "echo", _boom)

    redis_client = create_redis_client()
    try:
        await _clean_queues(redis_client)
        async with async_session_factory() as session:
            run = await request_agent_analysis(
                session,
                redis_client,
                project_id=project.id,
                agent_type="echo",
                prompt="重试我",
            )
        # 重试必须沿用初始任务载荷，避免输入在重投过程中漂移。
        first = await dequeue(redis_client, timeout=2)
        assert first is not None
        payload = first["payload"]

        for attempt, expected_delay in enumerate([30.0, 60.0, 120.0]):
            before = time.time()
            await handle_task({**first, "payload": payload}, redis_client)
            current = await _get_run(run.id)
            assert current.status == "pending"
            assert current.retry_count == attempt + 1
            assert current.error is not None and "ModelTimeoutError" in current.error
            assert current.duration_ms is not None

            entries = await redis_client.zrange(DELAYED_QUEUE_KEY, 0, -1, withscores=True)
            assert len(entries) == 1
            score = entries[0][1]
            assert before + expected_delay - 5 < score <= before + expected_delay + 1
            retried = json.loads(entries[0][0])
            assert retried["payload"]["run_id"] == str(run.id)

            # 显式推进时间，避免测试真实等待退避间隔。
            assert await promote_due_delayed(redis_client, now=before + expected_delay + 1) == 1
            next_task = await dequeue(redis_client, timeout=2)
            assert next_task is not None
            payload = next_task["payload"]

        await handle_task({**first, "payload": payload}, redis_client)
        final = await _get_run(run.id)
        assert final.status == "failed"
        assert final.retry_count == 3
        assert final.error is not None and "ModelTimeoutError" in final.error
        assert await redis_client.zcard(DELAYED_QUEUE_KEY) == 0
    finally:
        await redis_client.aclose()


async def test_validation_error_is_not_retried(
    project: Project, leader, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    """确定性 Schema 错误应直接失败，不得进入延迟重试队列。"""

    def _bad_output(state):  # noqa: ANN001, ANN202
        return {"not": "a valid suggestion"}  # 缺少必填字段，触发确定性校验错误。

    monkeypatch.setitem(graph_base.CAPABILITIES, "echo", _bad_output)

    redis_client = create_redis_client()
    try:
        await _clean_queues(redis_client)
        async with async_session_factory() as session:
            run = await request_agent_analysis(
                session, redis_client, project_id=project.id, agent_type="echo"
            )

        await handle_task(_run_task(run.id), redis_client)

        final = await _get_run(run.id)
        assert final.status == "failed"
        assert final.retry_count == 0
        assert final.error is not None and "schema_validate" in final.error
        assert await redis_client.zcard(DELAYED_QUEUE_KEY) == 0

        async with async_session_factory() as session:
            suggestion_count = (
                await session.execute(select(func.count()).select_from(AgentSuggestion))
            ).scalar_one()
        assert suggestion_count == 0
    finally:
        await redis_client.aclose()


async def test_worker_survives_agent_failure_and_core_flows_unaffected(
    client: httpx.AsyncClient, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型持续不可用时 worker 应继续处理任务，核心 API 仍保持可用。"""
    _, leader = await add_member(project, "leader", LEADER_PW, role="leader", display_name="负责人")
    monkeypatch.setattr(settings, "agent_run_max_retries", 0)

    def _boom(state):  # noqa: ANN001, ANN202
        raise ModelUnavailableError("ollama connection refused")

    monkeypatch.setitem(graph_base.CAPABILITIES, "echo", _boom)

    redis_client = create_redis_client()
    try:
        await _clean_queues(redis_client)
        async with async_session_factory() as session:
            failed_run = await request_agent_analysis(
                session, redis_client, project_id=project.id, agent_type="echo"
            )

        # 任务边界必须捕获 Agent 异常，避免终止 worker 循环。
        await safe_handle_task(_run_task(failed_run.id), redis_client)
        assert (await _get_run(failed_run.id)).status == "failed"

        # 未预期的处理器异常同样不得逃逸任务边界。
        async def _unexpected(payload, client_):  # noqa: ANN001, ANN202
            raise RuntimeError("unexpected processor error")

        monkeypatch.setattr(worker_module, "execute_agent_run", _unexpected)
        await safe_handle_task(_run_task(failed_run.id), redis_client)  # 不抛异常
        monkeypatch.undo()  # 恢复 execute_agent_run 与 echo 能力

        # 一次失败后，后续 Agent 和非 Agent 任务仍应正常消费。
        async with async_session_factory() as session:
            ok_run = await request_agent_analysis(
                session, redis_client, project_id=project.id, agent_type="echo"
            )
        await safe_handle_task(_run_task(ok_run.id), redis_client)
        assert (await _get_run(ok_run.id)).status == "succeeded"
        await safe_handle_task(
            {"id": str(uuid.uuid4()), "type": "example.ping", "payload": {"source": "test"}},
            redis_client,
        )

        # 登录和工作项流程不能依赖模型服务可用性。
        headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id))
        created = await client.post(
            "/api/v1/work-items",
            json={"title": "模型下线期间的工作项", "description": "核心业务不受影响", "assignee_id": str(leader.id)},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        listed = await client.get("/api/v1/work-items", headers=headers)
        assert listed.status_code == 200
    finally:
        await redis_client.aclose()


async def _make_failed_run(
    redis_client,  # noqa: ANN001
    work_item_id: uuid.UUID | None,
    prompt: str = "",
    *,
    project_id: uuid.UUID | None,
) -> AgentRun:
    """创建带项目归属且不自动重试的失败运行。"""
    async with async_session_factory() as session:
        run = await request_agent_analysis(
            session,
            redis_client,
            agent_type="echo",
            project_id=project_id,
            work_item_id=work_item_id,
            prompt=prompt,
        )
    await handle_task(_run_task(run.id, prompt), redis_client)
    failed = await _get_run(run.id)
    assert failed.status == "failed"
    return failed


async def test_manual_retry_failed_run_succeeds(
    client: httpx.AsyncClient, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """人工重试应重置失败状态、保留原输入并复用运行 ID。"""
    _, leader = await add_member(project, "leader", LEADER_PW, role="leader", display_name="负责人")
    monkeypatch.setattr(settings, "agent_run_max_retries", 0)

    def _boom(state):  # noqa: ANN001, ANN202
        raise ModelUnavailableError("ollama down")

    monkeypatch.setitem(graph_base.CAPABILITIES, "echo", _boom)

    redis_client = create_redis_client()
    try:
        await _clean_queues(redis_client)
        async with async_session_factory() as session:
            item = WorkItem(title="RAG 工作项", description="d", project_id=leader.project_id,
                            assignee_id=leader.id, status="READY")
            item.collaborators = []
            session.add(item)
            await session.commit()
            item_id = item.id

        run = await _make_failed_run(
            redis_client, item_id, prompt="做一个 RAG 问答", project_id=project.id
        )

        headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id))
        resp = await client.post(f"/api/v1/agent-runs/{run.id}/retry", headers=headers)
        assert resp.status_code == 202, resp.text
        assert resp.json()["status"] == "pending"

        retried = await _get_run(run.id)
        assert retried.status == "pending"
        assert retried.error is None
        assert retried.retry_count == 0
        assert retried.duration_ms is None

        # 人工重试从持久化运行恢复原输入。
        task = await dequeue(redis_client, timeout=2)
        assert task is not None
        assert task["type"] == "agent.run"
        assert task["payload"]["run_id"] == str(run.id)
        assert task["payload"]["prompt"] == "做一个 RAG 问答"

        # 模型恢复后复用同一运行，且只能生成一条建议。
        monkeypatch.undo()
        await handle_task(task, redis_client)
        final = await _get_run(run.id)
        assert final.status == "succeeded"
        async with async_session_factory() as session:
            suggestions = list(
                (
                    await session.execute(
                        select(AgentSuggestion).where(AgentSuggestion.run_id == run.id)
                    )
                )
                .scalars()
                .all()
            )
        assert len(suggestions) == 1
    finally:
        await redis_client.aclose()


async def test_manual_retry_permissions_and_status(
    client: httpx.AsyncClient, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """人工重试应限制运行状态和成员权限，项目级运行仅负责人可操作。"""
    _, leader = await add_member(project, "leader", LEADER_PW, role="leader", display_name="负责人")
    _, alice = await add_member(project, "alice", ALICE_PW, display_name="爱丽丝")
    _, bob = await add_member(project, "bob", BOB_PW, display_name="鲍勃")
    monkeypatch.setattr(settings, "agent_run_max_retries", 0)

    def _boom(state):  # noqa: ANN001, ANN202
        raise ModelUnavailableError("ollama down")

    monkeypatch.setitem(graph_base.CAPABILITIES, "echo", _boom)

    redis_client = create_redis_client()
    try:
        await _clean_queues(redis_client)
        async with async_session_factory() as session:
            item = WorkItem(title="RAG 工作项", description="d", project_id=alice.project_id,
                            assignee_id=alice.id, status="READY")
            item.collaborators = []
            session.add(item)
            await session.commit()
            item_id = item.id

        failed_run = await _make_failed_run(redis_client, item_id, project_id=project.id)
        project_run = await _make_failed_run(
            redis_client, None, project_id=project.id
        )
        async with async_session_factory() as session:
            pending_run = await request_agent_analysis(
                session, redis_client, agent_type="echo", project_id=project.id
            )

        leader_headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id))
        alice_headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id))
        bob_headers = await auth_headers(client, "bob", BOB_PW, project_id=str(project.id))

        resp = await client.post(f"/api/v1/agent-runs/{failed_run.id}/retry", headers=alice_headers)
        assert resp.status_code == 202, resp.text
        # 再次制造失败，以继续验证后续权限分支。
        await redis_client.delete(QUEUE_KEY)
        await handle_task(_run_task(failed_run.id), redis_client)
        assert (await _get_run(failed_run.id)).status == "failed"

        resp = await client.post(f"/api/v1/agent-runs/{failed_run.id}/retry", headers=bob_headers)
        assert resp.status_code == 403
        assert resp.json()["code"] == "FORBIDDEN"

        # 项目级运行没有工作项关系可授权，因此仅负责人可重试。
        resp = await client.post(f"/api/v1/agent-runs/{project_run.id}/retry", headers=alice_headers)
        assert resp.status_code == 403
        resp = await client.post(f"/api/v1/agent-runs/{project_run.id}/retry", headers=leader_headers)
        assert resp.status_code == 202, resp.text
        await redis_client.delete(QUEUE_KEY)

        resp = await client.post(f"/api/v1/agent-runs/{pending_run.id}/retry", headers=leader_headers)
        assert resp.status_code == 409
        assert resp.json()["code"] == "AGENT_RUN_NOT_FAILED"
        resp = await client.post(f"/api/v1/agent-runs/{project_run.id}/retry", headers=leader_headers)
        assert resp.status_code == 409

        resp = await client.post(f"/api/v1/agent-runs/{uuid.uuid4()}/retry", headers=leader_headers)
        assert resp.status_code == 404
    finally:
        await redis_client.aclose()
