"""Agent 记忆检索工具测试（M6.1 验收，设计文档第 11 节）。

- search_project_documents / search_history_records 走 M2.9 带权限路径
  （caller=agent_assignment），返回片段；
- 项目隔离：Agent 只命中当前项目的块；
- 工具注册进 TOOL_REGISTRY 且为 read_query（护栏断言在 test_agent_guardrails）。
"""

import uuid

import pytest

from app.agents.tools import (
    TOOL_REGISTRY,
    search_history_records,
    search_project_documents,
)
from app.domains.memory import indexer as indexer_module
from app.domains.memory import retriever as retriever_module
from app.domains.memory.indexer import MemoryIndexService
from app.domains.project.models import Project
from app.infrastructure.database.engine import async_session_factory
from tests.test_file_index_pipeline import FakeEmbeddingProvider


@pytest.fixture(autouse=True)
def fake_embedding(monkeypatch: pytest.MonkeyPatch):
    """索引写入与查询向量都用 fake provider（常数向量，余弦距离恒 0）。"""
    monkeypatch.setattr(
        indexer_module, "get_embedding_provider", lambda: FakeEmbeddingProvider()
    )
    monkeypatch.setattr(
        retriever_module, "get_embedding_provider", lambda: FakeEmbeddingProvider()
    )


async def _index(project: Project, source_type: str, text: str) -> uuid.UUID:
    source_id = uuid.uuid4()
    async with async_session_factory() as session:
        await MemoryIndexService(session).rebuild_chunks(
            project_id=project.id, source_type=source_type, source_id=source_id, text=text
        )
    return source_id


async def test_search_project_documents_tool(project_a: Project, project_b: Project) -> None:
    doc_id = await _index(project_a, "document", "发布步骤：先构建镜像再滚动重启")

    async with async_session_factory() as session:
        hits = await search_project_documents(
            session, "怎么部署", project_id=project_a.id
        )
        assert len(hits) == 1
        assert "发布步骤" in hits[0]["content"]  # 语义命中（用词不同也能命中）
        assert hits[0]["source_id"] == str(doc_id)

        # 项目隔离：B 项目视角检索不到 A 的文档
        assert await search_project_documents(session, "怎么部署", project_id=project_b.id) == []

        # 类型过滤：history 块不出现在文档检索里
        await MemoryIndexService(session).rebuild_chunks(
            project_id=project_a.id,
            source_type="history",
            source_id=uuid.uuid4(),
            text="上次发布需求拆成了两个工作项",
        )
        hits = await search_project_documents(session, "发布", project_id=project_a.id)
        assert all("发布步骤" in h["content"] for h in hits)


async def test_search_history_records_tool(project_a: Project, project_b: Project) -> None:
    history_id = await _index(
        project_a, "history", "需求拆解记录：导入功能上次拆成 4 个工作项，后端占比最大"
    )

    async with async_session_factory() as session:
        hits = await search_history_records(session, "导入怎么做", project_id=project_a.id)
        assert len(hits) == 1
        assert hits[0]["source_id"] == str(history_id)
        assert "拆成 4 个工作项" in hits[0]["content"]
        assert await search_history_records(session, "导入", project_id=project_b.id) == []


def test_tools_registered_as_read_query() -> None:
    for name in ("search_project_documents", "search_history_records"):
        tool = TOOL_REGISTRY[name]
        assert tool.kind == "read_query"
