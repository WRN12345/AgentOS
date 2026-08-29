"""验证 Agent 运行、建议记录与 LangGraph 基础图的集成行为。

成功运行应持久化耗时、检查点和建议，并向负责人发送站内通知；图节点失败时
应记录错误且不得产生建议或通知。
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
    """数据库迁移后应存在 Agent 运行表和建议表。"""
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
    """人工触发的分析应经队列执行并生成建议、通知和检查点。"""
    item = await _make_work_item(leader.id, project_id=project.id)
    redis_client = create_redis_client()
    try:
        # 隔离共享队列，避免其他用例遗留的任务影响本次消费。
        await redis_client.delete(QUEUE_KEY)
        async with async_session_factory() as session:
            run = await request_agent_analysis(
                session,
                redis_client,
                project_id=project.id,
                agent_type="echo",
                trigger_source="manual",
                work_item_id=item.id,
                prompt="把「实现用户登录」整理为结构化需求",
                request_id="req-test-1",
            )
        assert run.status == "pending"

        task = await dequeue(redis_client, timeout=2)
        assert task is not None
        assert task["type"] == "agent.run"
        assert task["payload"]["run_id"] == str(run.id)

        # 直接调用处理函数，避免测试依赖独立 worker 进程。
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

            # 检查点以 run_id 作为 thread_id，便于按运行恢复图状态。
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
    """图节点失败时应记录终态错误，且不得产生建议或通知。

    此处关闭自动重试以隔离终态语义；指数退避由重试测试单独覆盖。
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
                project_id=project.id,
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
