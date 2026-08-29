"""已完成工作项结论的索引投递、文本生成与写入测试。

- 审核通过（approve → COMPLETED）自动投递结论索引任务，不阻塞主流程；
- 结论文本含做了什么（标题/描述/验收标准）、验收结果与评审意见（含反馈正文）；
- 未完成的工作项不索引；worker 端到端写块可检索。
"""

import json
import uuid

import httpx
import pytest
from sqlalchemy import func, select

from app.domains.memory import indexer as indexer_module
from app.domains.memory.history import build_work_item_conclusion_text
from app.domains.memory.models import MemoryChunk
from app.domains.project.models import Project
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.queue.queue import DELAYED_QUEUE_KEY, QUEUE_KEY
from app.workers.memory_index import execute_memory_index
from tests.test_file_index_pipeline import FakeEmbeddingProvider
from tests.test_reviews_api import _item_in_review, _review, _setup


@pytest.fixture
async def redis_client():
    client = create_redis_client()
    await client.delete(QUEUE_KEY, DELAYED_QUEUE_KEY)
    yield client
    await client.delete(QUEUE_KEY, DELAYED_QUEUE_KEY)
    await client.aclose()


async def test_approve_dispatches_conclusion_index_task(
    client: httpx.AsyncClient, project: Project, redis_client
) -> None:
    ctx = await _setup(client, project)
    item_id, deliverable_id = await _item_in_review(client, ctx)

    resp = await _review(
        client,
        ctx["leader_headers"],  # type: ignore[arg-type]
        item_id,
        {"deliverable_id": deliverable_id, "decision": "approve", "feedback": "做得好"},
    )
    assert resp.status_code == 201, resp.text

    queued = await redis_client.lrange(QUEUE_KEY, 0, -1)
    tasks = [json.loads(t) for t in queued]
    history_tasks = [
        t
        for t in tasks
        if t["type"] == "memory.index"
        and t["payload"].get("history_kind") == "work_item"
    ]
    assert len(history_tasks) == 1
    assert history_tasks[0]["payload"]["source_id"] == item_id
    assert history_tasks[0]["payload"]["project_id"] == str(project.id)


async def test_non_completing_decision_no_task(
    client: httpx.AsyncClient, project: Project, redis_client
) -> None:
    """要求修改使工作项回到执行中时不得投递结论索引。"""
    ctx = await _setup(client, project)
    item_id, deliverable_id = await _item_in_review(client, ctx)

    resp = await _review(
        client,
        ctx["leader_headers"],  # type: ignore[arg-type]
        item_id,
        {"deliverable_id": deliverable_id, "decision": "request_changes", "feedback": "要改"},
    )
    assert resp.status_code == 201, resp.text
    # 队列可能包含交付评审任务，因此只排除工作项结论索引任务。
    queued = await redis_client.lrange(QUEUE_KEY, 0, -1)
    assert not [
        t
        for t in (json.loads(x) for x in queued)
        if t["type"] == "memory.index" and t["payload"].get("history_kind") == "work_item"
    ]


async def test_build_conclusion_text_completed_item(
    client: httpx.AsyncClient, project: Project, redis_client
) -> None:
    ctx = await _setup(client, project)
    item_id, deliverable_id = await _item_in_review(client, ctx)
    resp = await _review(
        client,
        ctx["leader_headers"],  # type: ignore[arg-type]
        item_id,
        {
            "deliverable_id": deliverable_id,
            "decision": "approve",
            "feedback": "实现完整，测试覆盖到位",
        },
    )
    assert resp.status_code == 201, resp.text

    async with async_session_factory() as session:
        text = await build_work_item_conclusion_text(session, uuid.UUID(item_id))
    assert text is not None
    assert "标题：RAG 工作项" in text
    assert "主执行人：爱丽丝" in text
    assert "做了什么：\n实现 RAG" in text
    assert "结论：通过" in text
    assert "评审意见：实现完整，测试覆盖到位" in text


async def test_build_conclusion_text_unfinished_returns_none(
    client: httpx.AsyncClient, project: Project, redis_client
) -> None:
    ctx = await _setup(client, project)
    item_id, _ = await _item_in_review(client, ctx)  # IN_REVIEW，未完成
    async with async_session_factory() as session:
        text = await build_work_item_conclusion_text(session, uuid.UUID(item_id))
    assert text is None


async def test_worker_indexes_conclusion_end_to_end(
    client: httpx.AsyncClient,
    project: Project,
    redis_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        indexer_module, "get_embedding_provider", lambda: FakeEmbeddingProvider()
    )
    ctx = await _setup(client, project)
    item_id, deliverable_id = await _item_in_review(client, ctx)
    await _review(
        client,
        ctx["leader_headers"],  # type: ignore[arg-type]
        item_id,
        {"deliverable_id": deliverable_id, "decision": "approve", "feedback": "通过"},
    )

    await execute_memory_index(
        {
            "source_type": "history",
            "source_id": item_id,
            "project_id": str(project.id),
            "history_kind": "work_item",
        },
        None,  # type: ignore[arg-type]
    )

    async with async_session_factory() as session:
        chunks = (
            await session.execute(
                select(MemoryChunk).where(
                    MemoryChunk.source_type == "history",
                    MemoryChunk.source_id == uuid.UUID(item_id),
                )
            )
        ).scalars().all()
    assert len(chunks) > 0
    assert all(c.project_id == project.id for c in chunks)
    assert any("RAG 工作项" in c.content for c in chunks)
