"""T5.6 Agent 失败恢复测试（17.3 节、第 22 章标准 9）。

覆盖：
- 退避间隔公式：base * 2^attempt（纯函数断言间隔序列）；
- 延迟队列：ZSET 定时投递，到点 promote 回即时队列；
- 模型持续超时/不可用：run 按指数间隔重试，retry_count 递增，
  超上限后终态 failed、error 可查；
- 确定性错误（Schema 校验失败）不重试，直接 failed；
- worker 韧性：Agent run 持续失败（模型不可用）不拖垮 worker，
  其他类型任务照常处理，核心业务 API（登录/工作项）不依赖模型；
- 人工重新触发 POST /agent-runs/{id}/retry：failed → 202 重新投递并
  成功完成（同一 run_id，不产生重复建议）；非 failed → 409；无关成员
  → 403；不存在 → 404。
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
    """清空共享测试队列（即时 List + 延迟 ZSET），避免用例间残留干扰。"""
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


# ---------- 退避间隔与延迟队列 ----------


def test_retry_delay_is_exponential(monkeypatch: pytest.MonkeyPatch) -> None:
    """间隔 = base * 2^attempt：base=30 → 30/60/120；base=5 → 5/10/20/40。"""
    monkeypatch.setattr(settings, "agent_run_retry_base_seconds", 30.0)
    assert [retry_delay_seconds(i) for i in range(3)] == [30.0, 60.0, 120.0]
    monkeypatch.setattr(settings, "agent_run_retry_base_seconds", 5.0)
    assert [retry_delay_seconds(i) for i in range(4)] == [5.0, 10.0, 20.0, 40.0]


async def test_delayed_queue_promotes_due_tasks() -> None:
    """ZSET 延迟投递：未到点不搬移，到点搬入即时队列后可 BRPOP 消费。"""
    redis_client = create_redis_client()
    try:
        await _clean_queues(redis_client)
        before = time.time()
        task = await enqueue_delayed(redis_client, "example.ping", {"source": "test"}, delay_seconds=60)
        entries = await redis_client.zrange(DELAYED_QUEUE_KEY, 0, -1, withscores=True)
        assert len(entries) == 1
        score = entries[0][1]
        assert before + 55 < score <= before + 60.5

        # 未到点：什么都不搬
        assert await promote_due_delayed(redis_client, now=before + 59) == 0
        assert await redis_client.llen(QUEUE_KEY) == 0
        # 到点：搬入即时队列
        assert await promote_due_delayed(redis_client, now=before + 61) == 1
        assert await redis_client.zcard(DELAYED_QUEUE_KEY) == 0
        dequeued = await dequeue(redis_client, timeout=2)
        assert dequeued is not None
        assert dequeued["id"] == task["id"]
    finally:
        await redis_client.aclose()


# ---------- 自动指数退避重试 ----------


async def test_model_timeout_retries_with_backoff_then_fails(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型持续超时：按 30/60/120 退避重试 3 次，第 4 次失败后终态 failed。"""
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
                session, redis_client, agent_type="echo", prompt="重试我"
            )
        # 初始投递的任务取回 payload（后续重投沿用同一 payload）
        first = await dequeue(redis_client, timeout=2)
        assert first is not None
        payload = first["payload"]

        # 前 3 次失败：回 pending、retry_count 递增、按 30/60/120 延迟重投
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
            assert retried["payload"]["run_id"] == str(run.id)  # 同一 run_id 重跑

            # 模拟到点：搬回即时队列并取出，作为下一次执行的任务
            assert await promote_due_delayed(redis_client, now=before + expected_delay + 1) == 1
            next_task = await dequeue(redis_client, timeout=2)
            assert next_task is not None
            payload = next_task["payload"]

        # 第 4 次失败：重试耗尽 → 终态 failed，错误可查，不再重投
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
    """确定性错误（Schema 校验失败）直接 failed：retry_count=0，不进延迟队列。"""

    def _bad_output(state):  # noqa: ANN001, ANN202
        return {"not": "a valid suggestion"}  # 缺字段 → validate_output 抛 SuggestionValidationError

    monkeypatch.setitem(graph_base.CAPABILITIES, "echo", _bad_output)

    redis_client = create_redis_client()
    try:
        await _clean_queues(redis_client)
        async with async_session_factory() as session:
            run = await request_agent_analysis(session, redis_client, agent_type="echo")

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


# ---------- worker 韧性（标准 9：Agent 失败不影响核心业务） ----------


async def test_worker_survives_agent_failure_and_core_flows_unaffected(
    client: httpx.AsyncClient, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型持续不可用：Agent run 失败被完全捕获，worker 继续处理后续任务；
    登录、工作项等核心 API 本就不依赖模型，全程可用。"""
    _, leader = await add_member(project, "leader", LEADER_PW, role="leader", display_name="负责人")
    monkeypatch.setattr(settings, "agent_run_max_retries", 0)

    def _boom(state):  # noqa: ANN001, ANN202
        raise ModelUnavailableError("ollama connection refused")

    monkeypatch.setitem(graph_base.CAPABILITIES, "echo", _boom)

    redis_client = create_redis_client()
    try:
        await _clean_queues(redis_client)
        async with async_session_factory() as session:
            failed_run = await request_agent_analysis(session, redis_client, agent_type="echo")

        # 1) Agent 任务失败被捕获（safe_handle_task 不向上抛）
        await safe_handle_task(_run_task(failed_run.id), redis_client)
        assert (await _get_run(failed_run.id)).status == "failed"

        # 2) 处理器自身抛出意外错误也不拖垮 worker（如数据库瞬断）
        async def _unexpected(payload, client_):  # noqa: ANN001, ANN202
            raise RuntimeError("unexpected processor error")

        monkeypatch.setattr(worker_module, "execute_agent_run", _unexpected)
        await safe_handle_task(_run_task(failed_run.id), redis_client)  # 不抛异常
        monkeypatch.undo()  # 恢复 execute_agent_run 与 echo 能力

        # 3) 后续任务照常处理：新的 Agent 运行成功，其他类型任务正常消费
        async with async_session_factory() as session:
            ok_run = await request_agent_analysis(session, redis_client, agent_type="echo")
        await safe_handle_task(_run_task(ok_run.id), redis_client)
        assert (await _get_run(ok_run.id)).status == "succeeded"
        await safe_handle_task(
            {"id": str(uuid.uuid4()), "type": "example.ping", "payload": {"source": "test"}},
            redis_client,
        )

        # 4) 模型不可用期间，核心流程（登录、建工作项）全部可用
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


# ---------- 人工重新触发 POST /agent-runs/{id}/retry ----------


async def _make_failed_run(
    redis_client,  # noqa: ANN001
    work_item_id: uuid.UUID | None,
    prompt: str = "",
    *,
    project_id: uuid.UUID | None,
) -> AgentRun:
    """建 run 并让其直接终态 failed（max_retries=0 + echo 不可用）。

    ticket 05：run 必须带项目归属，否则重试端点按项目过滤时视为不存在。
    """
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
    """failed → leader 202 重新投递（状态/错误/retry_count 重置，原 prompt 保留），
    worker 消费后成功完成：同一 run_id，不产生重复建议。"""
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

        # 队列任务保留原输入（prompt 取自 agent_runs 持久化字段）
        task = await dequeue(redis_client, timeout=2)
        assert task is not None
        assert task["type"] == "agent.run"
        assert task["payload"]["run_id"] == str(run.id)
        assert task["payload"]["prompt"] == "做一个 RAG 问答"

        # worker 消费重试任务（echo 已恢复）→ 成功；同一 run_id，建议不重复
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
    """权限与状态：相关成员 202；无关成员 403；非 failed（pending/succeeded）409；
    不存在 404；项目级 run（无工作项）仅负责人可重试。"""
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
        )  # 项目级 run（无工作项），归属显式指定
        async with async_session_factory() as session:
            pending_run = await request_agent_analysis(
                session, redis_client, agent_type="echo", project_id=project.id
            )

        leader_headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id))
        alice_headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id))
        bob_headers = await auth_headers(client, "bob", BOB_PW, project_id=str(project.id))

        # 相关成员（主执行人 alice）→ 202
        resp = await client.post(f"/api/v1/agent-runs/{failed_run.id}/retry", headers=alice_headers)
        assert resp.status_code == 202, resp.text
        # 重试后状态回 pending，重新失败一次供后续用例
        await redis_client.delete(QUEUE_KEY)
        await handle_task(_run_task(failed_run.id), redis_client)
        assert (await _get_run(failed_run.id)).status == "failed"

        # 无关成员 bob → 403
        resp = await client.post(f"/api/v1/agent-runs/{failed_run.id}/retry", headers=bob_headers)
        assert resp.status_code == 403
        assert resp.json()["code"] == "FORBIDDEN"

        # 项目级 run：非负责人一律 403（无工作项可关联），负责人 202
        resp = await client.post(f"/api/v1/agent-runs/{project_run.id}/retry", headers=alice_headers)
        assert resp.status_code == 403
        resp = await client.post(f"/api/v1/agent-runs/{project_run.id}/retry", headers=leader_headers)
        assert resp.status_code == 202, resp.text
        await redis_client.delete(QUEUE_KEY)

        # 非 failed 状态 → 409（pending；以及刚被 alice 重试过的 pending 态 failed_run）
        resp = await client.post(f"/api/v1/agent-runs/{pending_run.id}/retry", headers=leader_headers)
        assert resp.status_code == 409
        assert resp.json()["code"] == "AGENT_RUN_NOT_FAILED"
        resp = await client.post(f"/api/v1/agent-runs/{project_run.id}/retry", headers=leader_headers)
        assert resp.status_code == 409

        # 不存在 → 404
        resp = await client.post(f"/api/v1/agent-runs/{uuid.uuid4()}/retry", headers=leader_headers)
        assert resp.status_code == 404
    finally:
        await redis_client.aclose()
