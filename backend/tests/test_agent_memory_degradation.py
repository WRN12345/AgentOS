"""记忆降级与标注测试（M6.6 验收，设计文档 16.5）。

- embedding/检索不可用：拆解分配照常完成，建议标注 memory_status=degraded
  并在 risks 中说明"本次未参考记忆"；
- 记忆服务正常：memory_status=ok，无降级标注。
"""

import json
import uuid

import pytest
from sqlalchemy import select

from app.agents.models import AgentSuggestion
from app.domains.memory import retriever as retriever_module
from app.domains.project.models import Project, ProjectMember
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.models.errors import ModelUnavailableError
from tests.test_agent_pipeline import (
    _breakdown_stage,
    _analysis_stage,
    _patch_provider,
    _run_once,
    _trigger,
    _ScriptedProvider,
)
from tests.test_file_index_pipeline import FakeEmbeddingProvider


def _assign_stage_null() -> str:
    """分配段脚本：两个拆解项均无合适人选（null 允许）。"""
    return json.dumps(
        {
            "assignments": [
                {"recommended_assignee": None, "candidates": [], "notes": ""},
                {"recommended_assignee": None, "candidates": [], "notes": ""},
            ],
            "risks": [],
        },
        ensure_ascii=False,
    )


class _RaisingEmbeddingProvider(FakeEmbeddingProvider):
    async def embed(self, texts):
        raise ModelUnavailableError("ollama down")


async def _run_pipeline(
    project: Project, redis_client, monkeypatch: pytest.MonkeyPatch, embedding
) -> AgentSuggestion:
    monkeypatch.setattr(
        retriever_module, "get_embedding_provider", lambda: embedding
    )
    provider = _ScriptedProvider(
        [_analysis_stage(), _breakdown_stage(), _assign_stage_null()]
    )
    _patch_provider(monkeypatch, provider)
    run = await _trigger(
        redis_client, "搭建 RAG 问答平台", project_id=project.id
    )
    await _run_once(redis_client, run.id, "搭建 RAG 问答平台")
    async with async_session_factory() as session:
        suggestions = list((await session.execute(select(AgentSuggestion))).scalars().all())
    assert len(suggestions) == 1
    return suggestions[0]


@pytest.fixture
async def redis_client():
    client = create_redis_client()
    yield client
    await client.aclose()


async def test_degraded_when_embedding_unavailable(
    project: Project, leader: ProjectMember, redis_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """停 Ollama：拆解/分配照常完成且带降级标注（16.5）。"""
    suggestion = await _run_pipeline(
        project, redis_client, monkeypatch, _RaisingEmbeddingProvider()
    )
    assert suggestion.content["memory_status"] == "degraded"
    assert any("本次未参考记忆" in r for r in suggestion.content["risks"])


async def test_normal_when_memory_available(
    project: Project, leader: ProjectMember, redis_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """记忆服务正常：标注 ok，无降级说明（恢复后标注消失）。"""
    suggestion = await _run_pipeline(
        project, redis_client, monkeypatch, FakeEmbeddingProvider()
    )
    assert suggestion.content["memory_status"] == "ok"
    assert not any("本次未参考记忆" in r for r in suggestion.content["risks"])
