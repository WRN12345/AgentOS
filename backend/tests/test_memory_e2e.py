"""端到端验收（M8.3，设计文档两条核心路径）。

路径①：上传文档 → 索引 → 问答命中 → 依据可查（下载原文）；
路径②：完成任务 → 总结提议 → 负责人确认 → 下次拆解时核心记忆在场。

以集成测试形式固化走查结果（可重复执行，即验收记录）。
模型与 embedding 用替身（FakeEmbeddingProvider / 脚本化 ModelProvider），
其余全链路（API → 队列 → worker → DB → 检索 → 问答/流水线）真实执行。
"""

import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.domains.memory import indexer as indexer_module
from app.domains.memory import qa as qa_module
from app.domains.memory import retriever as retriever_module
from app.domains.memory import summary as summary_module
from app.agents.models import AgentSuggestion
from app.domains.project.models import Project
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.queue.queue import DELAYED_QUEUE_KEY, QUEUE_KEY
from app.workers import memory_index as memory_index_module
from app.infrastructure.storage.local import LocalStorageProvider
from app.infrastructure.storage.provider import get_storage_provider
from app.main import app
from app.workers.memory_index import execute_memory_index
from app.workers.memory_summary import execute_memory_summary
from tests.conftest import add_member, auth_headers
from tests.test_agent_pipeline import (
    _analysis_stage,
    _assign_stage,
    _breakdown_stage,
    _memory_none_stage,
    _patch_provider,
    _run_once,
    _ScriptedProvider,
    _trigger,
)
from tests.test_file_index_pipeline import FakeEmbeddingProvider
from tests.test_memory_qa import _ScriptedQAProvider
from tests.test_reviews_api import _item_in_review, _review, _setup

LEADER_PW = "Leader123!"


@pytest.fixture
def storage(tmp_path: Path):
    provider = LocalStorageProvider(tmp_path)
    app.dependency_overrides[get_storage_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_storage_provider, None)


@pytest.fixture
async def redis_client():
    client = create_redis_client()
    await client.delete(QUEUE_KEY, DELAYED_QUEUE_KEY)
    yield client
    await client.delete(QUEUE_KEY, DELAYED_QUEUE_KEY)
    await client.aclose()


@pytest.fixture(autouse=True)
def fake_embedding(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        indexer_module, "get_embedding_provider", lambda: FakeEmbeddingProvider()
    )
    monkeypatch.setattr(
        retriever_module, "get_embedding_provider", lambda: FakeEmbeddingProvider()
    )


async def _take_tasks(redis_client, task_type: str) -> list[dict]:
    return [
        t
        for t in (json.loads(x) for x in await redis_client.lrange(QUEUE_KEY, 0, -1))
        if t["type"] == task_type
    ]


async def test_path1_upload_index_qa_source(
    client: httpx.AsyncClient, project_a: Project, storage, redis_client, monkeypatch
) -> None:
    """路径①：上传文档 → 索引 → 问答命中 → 依据可查。"""
    _, alice = await add_member(project_a, "alice", "Alice123!", display_name="爱丽丝")
    headers = await auth_headers(client, "alice", "Alice123!", project_id=str(project_a.id))

    # 1. 上传文档（自动投递索引任务）
    resp = await client.post(
        "/api/v1/files",
        headers=headers,
        files={"file": ("部署指南.md", "发布步骤：先构建镜像再滚动重启。".encode(), "text/markdown")},
    )
    assert resp.status_code == 201, resp.text
    file_id = resp.json()["id"]

    # 2. worker 执行索引 → 状态已索引（worker 侧 storage 指向测试临时目录）
    original = memory_index_module.get_storage_provider
    memory_index_module.get_storage_provider = lambda: storage
    try:
        tasks = await _take_tasks(redis_client, "memory.index")
        assert len(tasks) == 1
        await execute_memory_index(tasks[0]["payload"], None)  # type: ignore[arg-type]
    finally:
        memory_index_module.get_storage_provider = original
    resp = await client.get("/api/v1/files", headers=headers)
    assert resp.json()[0]["index_status"] == "indexed"

    # 3. 问答命中（模型用脚本替身）
    monkeypatch.setattr(
        qa_module,
        "get_model_provider",
        lambda: _ScriptedQAProvider("发布前先构建镜像再滚动重启 [1]。"),
    )
    resp = await client.post(
        "/api/v1/memory/qa", headers=headers, json={"question": "怎么部署"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "answered"
    assert body["sources"][0]["title"] == "部署指南.md"  # 依据可定位到文件
    assert body["sources"][0]["source_id"] == file_id

    # 4. 依据可查：按 source_id 下载原文
    resp = await client.get(f"/api/v1/files/{file_id}/download", headers=headers)
    assert resp.status_code == 200
    assert "发布步骤" in resp.content.decode()


async def test_path2_complete_summarize_confirm_inject(
    client: httpx.AsyncClient, project_a: Project, storage, redis_client, monkeypatch
) -> None:
    """路径②：完成任务 → 总结提议 → 确认 → 下次拆解在场。"""
    ctx = await _setup(client, project_a)
    item_id, deliverable_id = await _item_in_review(client, ctx)

    # 1. 审核通过（完成）→ 自动投递总结任务
    resp = await _review(
        client,
        ctx["leader_headers"],  # type: ignore[arg-type]
        item_id,
        {"deliverable_id": deliverable_id, "decision": "approve", "feedback": "做得好"},
    )
    assert resp.status_code == 201, resp.text
    summary_tasks = await _take_tasks(redis_client, "memory.summary")
    assert len(summary_tasks) == 1

    # 2. worker 执行总结（模型替身产出经验）→ 生成待确认提议
    monkeypatch.setattr(
        summary_module,
        "get_model_provider",
        lambda: _ScriptedQAProvider("RAG 类工作项评审一次通过的关键是验收标准前置"),
    )
    await execute_memory_summary(summary_tasks[0]["payload"])
    leader_headers = ctx["leader_headers"]  # type: ignore[assignment]
    resp = await client.get(
        "/api/v1/agent-suggestions",
        headers=leader_headers,  # type: ignore[arg-type]
        params={"suggestion_type": "memory_proposal"},
    )
    proposals = resp.json()
    assert len(proposals) == 1
    assert proposals[0]["review_status"] == "pending"
    assert "验收标准前置" in proposals[0]["content"]["content"]

    # 3. 负责人确认 → 进入核心记忆
    resp = await client.post(
        f"/api/v1/agent-suggestions/{proposals[0]['id']}/feedback",
        headers=leader_headers,  # type: ignore[arg-type]
        json={"action": "accepted"},
    )
    assert resp.status_code == 200, resp.text
    resp = await client.get(
        "/api/v1/memory/core-entries", headers=leader_headers  # type: ignore[arg-type]
    )
    entries = resp.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["status"] == "active"
    assert "验收标准前置" in entries[0]["content"]

    # 4. 下次拆解：核心记忆全量注入流水线提示词（在场）
    _, zhangsan = await add_member(project_a, "zhangsan", "Zhang123!", display_name="张三")
    provider = _ScriptedProvider(
        [
            _analysis_stage(),
            _breakdown_stage(),
            _assign_stage(zhangsan, zhangsan),
            _memory_none_stage(),
        ]
    )
    _patch_provider(monkeypatch, provider)
    run = await _trigger(redis_client, "再做一个 RAG 功能", project_id=project_a.id)
    await _run_once(redis_client, run.id, "再做一个 RAG 功能")

    injected = [c["prompt"] for c in provider.calls if "项目核心记忆" in c["prompt"]]
    assert injected, "下次拆解的提示词中应包含核心记忆注入块"
    assert any("验收标准前置" in p for p in injected)  # 确认的经验在场
    # 主建议正常产出
    async with async_session_factory() as session:
        count = list(
            (
                await session.execute(
                    select(AgentSuggestion).where(
                        AgentSuggestion.suggestion_type == "pipeline"
                    )
                )
            ).scalars().all()
        )
    assert len(count) == 1
