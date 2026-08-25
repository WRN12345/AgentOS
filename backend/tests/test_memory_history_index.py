"""拆解/分配记录入索引测试（M5.1 验收，设计文档第 9 节）。

- 已完成（run succeeded + 反馈落定）的拆解记录可被检索：文本含输入需求、
  摘要、采纳情况、拆解方案要点；
- 进行中的不索引（15.4）：pending/running run 与 echo 等非拆解类型返回 None；
- 反馈落定自动投递 memory.index（source_type=history），worker 从 run 现取
  文本整体重建索引块。
"""

import json

import httpx
import pytest
from sqlalchemy import func, select

from app.agents.models import AgentRun, AgentSuggestion
from app.domains.memory import indexer as indexer_module
from app.domains.memory.history import build_run_history_text
from app.domains.memory.models import MemoryChunk
from app.domains.project.models import Project, ProjectMember
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.queue.queue import DELAYED_QUEUE_KEY, QUEUE_KEY
from app.workers.memory_index import execute_memory_index
from tests.conftest import auth_headers
from tests.test_file_index_pipeline import FakeEmbeddingProvider

LEADER_PW = "Leader123!"


@pytest.fixture
async def redis_client():
    client = create_redis_client()
    await client.delete(QUEUE_KEY, DELAYED_QUEUE_KEY)
    yield client
    await client.delete(QUEUE_KEY, DELAYED_QUEUE_KEY)
    await client.aclose()


async def _make_run(
    project_id,
    *,
    agent_type: str = "requirement_pipeline",
    status: str = "succeeded",
    prompt: str = "做一个导入功能",
) -> AgentRun:
    async with async_session_factory() as session:
        run = AgentRun(
            agent_type=agent_type, project_id=project_id, status=status, prompt=prompt
        )
        session.add(run)
        await session.commit()
        return run


async def _add_suggestion(
    run: AgentRun,
    *,
    suggestion_type: str = "pipeline",
    review_status: str = "pending",
    content: dict | None = None,
) -> AgentSuggestion:
    async with async_session_factory() as session:
        suggestion = AgentSuggestion(
            run_id=run.id,
            suggestion_type=suggestion_type,
            content=content
            or {
                "summary": "拆成 4 个工作项",
                "rationale": "后端占比最大",
                "work_item_breakdown": [
                    {
                        "title": "导入接口",
                        "recommended_assignee": {"display_name": "爱丽丝"},
                    },
                    {"title": "导入向导页"},
                ],
            },
            review_status=review_status,
        )
        session.add(suggestion)
        await session.commit()
        return suggestion


async def test_build_text_completed_run(project_a: Project) -> None:
    run = await _make_run(project_a.id)
    await _add_suggestion(run, review_status="accepted")

    async with async_session_factory() as session:
        text = await build_run_history_text(session, run.id)

    assert text is not None
    assert "输入需求：\n做一个导入功能" in text
    assert "摘要：拆成 4 个工作项" in text
    assert "已采纳" in text
    assert "- 导入接口（推荐：爱丽丝）" in text
    assert "- 导入向导页" in text


async def test_build_text_excludes_pending_suggestions(project_a: Project) -> None:
    """首次反馈重建历史时，未确认的同运行建议不能进入可检索文本。"""
    run = await _make_run(project_a.id)
    await _add_suggestion(
        run,
        review_status="accepted",
        content={"summary": "已确认的拆解方案", "rationale": "负责人已采纳"},
    )
    await _add_suggestion(
        run,
        review_status="pending",
        content={"summary": "待确认的敏感建议", "rationale": "尚未人工确认"},
    )

    async with async_session_factory() as session:
        text = await build_run_history_text(session, run.id)

    assert text is not None
    assert "已确认的拆解方案" in text
    assert "待确认的敏感建议" not in text
    assert "尚未人工确认" not in text
    assert "待反馈" not in text


async def test_build_text_skips_unfinished_and_other_types(project_a: Project) -> None:
    running = await _make_run(project_a.id, status="running")
    echo = await _make_run(project_a.id, agent_type="echo")
    async with async_session_factory() as session:
        assert await build_run_history_text(session, running.id) is None
        assert await build_run_history_text(session, echo.id) is None


async def test_feedback_dispatches_history_index_task(
    client: httpx.AsyncClient, project_a: Project, leader: ProjectMember, redis_client
) -> None:
    run = await _make_run(project_a.id)
    suggestion = await _add_suggestion(run)

    headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project_a.id))
    resp = await client.post(
        f"/api/v1/agent-suggestions/{suggestion.id}/feedback",
        headers=headers,
        json={"action": "accepted"},
    )
    assert resp.status_code == 200, resp.text

    queued = await redis_client.lrange(QUEUE_KEY, 0, -1)
    tasks = [json.loads(t) for t in queued]
    history_tasks = [
        t
        for t in tasks
        if t["type"] == "memory.index" and t["payload"]["source_type"] == "history"
    ]
    assert len(history_tasks) == 1
    assert history_tasks[0]["payload"]["source_id"] == str(run.id)
    assert history_tasks[0]["payload"]["project_id"] == str(project_a.id)


async def test_feedback_on_non_history_run_no_task(
    client: httpx.AsyncClient, project_a: Project, leader: ProjectMember, redis_client
) -> None:
    """非拆解/分配类型（如风险扫描）的反馈不触发历史索引。"""
    run = await _make_run(project_a.id, agent_type="workflow_risk")
    suggestion = await _add_suggestion(run, suggestion_type="risk", content={"summary": "x"})

    headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project_a.id))
    resp = await client.post(
        f"/api/v1/agent-suggestions/{suggestion.id}/feedback",
        headers=headers,
        json={"action": "ignored"},
    )
    assert resp.status_code == 200, resp.text
    assert await redis_client.llen(QUEUE_KEY) == 0


async def test_worker_indexes_history_end_to_end(
    project_a: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """worker 全流程：history 任务从 run 现取文本 → 切块入库，检索源可定位。"""
    monkeypatch.setattr(
        indexer_module, "get_embedding_provider", lambda: FakeEmbeddingProvider()
    )
    run = await _make_run(project_a.id)
    await _add_suggestion(run, review_status="accepted")

    await execute_memory_index(
        {"source_type": "history", "source_id": str(run.id), "project_id": str(project_a.id)},
        None,  # type: ignore[arg-type]
    )

    async with async_session_factory() as session:
        chunks = (
            await session.execute(
                select(MemoryChunk).where(
                    MemoryChunk.source_type == "history",
                    MemoryChunk.source_id == run.id,
                )
            )
        ).scalars().all()
    assert len(chunks) > 0
    assert all(c.project_id == project_a.id for c in chunks)
    assert any("做一个导入功能" in c.content for c in chunks)


async def test_worker_skips_unfinished_run(
    project_a: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """进行中的运行不索引（15.4）：worker 跳过，无块写入。"""
    monkeypatch.setattr(
        indexer_module, "get_embedding_provider", lambda: FakeEmbeddingProvider()
    )
    run = await _make_run(project_a.id, status="running")

    await execute_memory_index(
        {"source_type": "history", "source_id": str(run.id), "project_id": str(project_a.id)},
        None,  # type: ignore[arg-type]
    )

    async with async_session_factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(MemoryChunk)
                .where(MemoryChunk.source_id == run.id)
            )
        ).scalar_one()
    assert count == 0
