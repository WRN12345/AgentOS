"""agent_runs/agent_suggestions 与 LangGraph 基础图测试（T5.2 验收，10.2、17.3 节）。

覆盖：
- 迁移后 agent_runs / agent_suggestions 建表成功；
- 人工触发 → 队列投递 agent.run → worker handle_task 执行图：
  agent_runs=succeeded（含耗时）、PostgreSQL 出现 langgraph checkpoint 行、
  生成 agent_suggestions 记录并产生站内通知（agent.suggestion_ready）；
- 图内节点抛错 → run 标记 failed + 错误信息，不产生建议。
"""

import uuid

import pytest
from sqlalchemy import func, select, text

from app.agents.graphs import base as graph_base
from app.agents.models import AgentRun, AgentSuggestion
from app.agents.service import request_agent_analysis
from app.core.config import settings
from app.domains.notifications.models import Notification
from app.domains.project.models import Project, ProjectMember
from app.domains.work_items.models import WorkItem
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.queue.queue import QUEUE_KEY, dequeue
from app.workers.worker import handle_task
from tests.conftest import add_member


async def _make_work_item(assignee_id: uuid.UUID, *, project_id: uuid.UUID) -> WorkItem:
    async with async_session_factory() as session:
        item = WorkItem(
            title="实现用户登录",
            description="支持账号密码登录",
            project_id=project_id,
            assignee_id=assignee_id,
            status="READY",
        )
        item.collaborators = []
        session.add(item)
        await session.commit()
        return item


async def test_agent_tables_exist() -> None:
    """迁移 0009 后 agent_runs / agent_suggestions 两表存在。"""
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE tablename IN ('agent_runs', 'agent_suggestions')"
                )
            )
        ).all()
    assert {r[0] for r in rows} == {"agent_runs", "agent_suggestions"}


async def test_agent_run_success_end_to_end(project: Project, leader: ProjectMember) -> None:
    """人工触发 echo 分析：队列 → worker → 图 → 建议 + 通知 + 检查点。"""
    item = await _make_work_item(leader.id, project_id=project.id)
    redis_client = create_redis_client()
    try:
        # 清空共享测试队列：其他用例（如 POST /tasks/example）可能留有残留任务
        await redis_client.delete(QUEUE_KEY)
        async with async_session_factory() as session:
            run = await request_agent_analysis(
                session,
                redis_client,
                agent_type="echo",
                trigger_source="manual",
                work_item_id=item.id,
                prompt="把「实现用户登录」整理为结构化需求",
                request_id="req-test-1",
            )
        assert run.status == "pending"

        # 队列里确实有 agent.run 任务（复用 T1.6 队列机制）
        task = await dequeue(redis_client, timeout=2)
        assert task is not None
        assert task["type"] == "agent.run"
        assert task["payload"]["run_id"] == str(run.id)

        # 测试里直接调用 worker 的处理函数（不真起进程）
        await handle_task(task, redis_client)

        async with async_session_factory() as session:
            final = await session.get(AgentRun, run.id)
            assert final is not None
            assert final.status == "succeeded"
            assert final.duration_ms is not None and final.duration_ms >= 0
            assert final.error is None
            assert final.request_id == "req-test-1"

            suggestions = list(
                (await session.execute(select(AgentSuggestion))).scalars().all()
            )
            assert len(suggestions) == 1
            suggestion = suggestions[0]
            assert suggestion.run_id == run.id
            assert suggestion.suggestion_type == "echo"
            assert suggestion.review_status == "pending"
            assert suggestion.prompt_version == graph_base.ECHO_PROMPT_VERSION
            assert "实现用户登录" in suggestion.content["echo"]["work_item_title"]
            assert suggestion.fact_refs == {"work_item_ids": [str(item.id)]}

            # 站内通知发给项目负责人（复用 T3.5 notify）
            notices = list(
                (
                    await session.execute(
                        select(Notification).where(Notification.recipient_id == leader.id)
                    )
                )
                .scalars()
                .all()
            )
            assert [n.type for n in notices] == ["agent.suggestion_ready"]
            assert notices[0].link == f"/work-items/{item.id}"

            # LangGraph 检查点已持久化到 PostgreSQL（thread_id = run_id）
            checkpoint_count = (
                await session.execute(
                    text("SELECT count(*) FROM checkpoints WHERE thread_id = :tid"),
                    {"tid": str(run.id)},
                )
            ).scalar_one()
            assert checkpoint_count >= 1
    finally:
        await redis_client.aclose()


async def test_agent_run_failure_marks_failed(
    project: Project, leader: ProjectMember, monkeypatch: pytest.MonkeyPatch
) -> None:
    """图内能力节点抛错 → agent_runs=failed + 错误信息，不产生建议/通知。

    T5.6 起可重试错误默认按指数退避重投；本用例只验证终态记录语义，
    故把自动重试次数设为 0（重试行为本身由 test_agent_retry.py 覆盖）。
    """
    monkeypatch.setattr(settings, "agent_run_max_retries", 0)

    def _boom(state):  # noqa: ANN001, ANN202
        raise RuntimeError("capability exploded")

    monkeypatch.setitem(graph_base.CAPABILITIES, "echo", _boom)

    redis_client = create_redis_client()
    try:
        async with async_session_factory() as session:
            run = await request_agent_analysis(
                session,
                redis_client,
                agent_type="echo",
                prompt="触发失败路径",
            )

        await handle_task(
            {
                "id": str(uuid.uuid4()),
                "type": "agent.run",
                "payload": {"run_id": str(run.id), "prompt": "触发失败路径"},
            },
            redis_client,
        )

        async with async_session_factory() as session:
            final = await session.get(AgentRun, run.id)
            assert final is not None
            assert final.status == "failed"
            assert final.error is not None and "capability exploded" in final.error
            assert final.duration_ms is not None

            suggestion_count = (
                await session.execute(select(func.count()).select_from(AgentSuggestion))
            ).scalar_one()
            assert suggestion_count == 0
            notification_count = (
                await session.execute(
                    select(func.count())
                    .select_from(Notification)
                    .where(Notification.type == "agent.suggestion_ready")
                )
            ).scalar_one()
            assert notification_count == 0
    finally:
        await redis_client.aclose()
