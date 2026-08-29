"""验证 Agent 记忆检索工具的结果、注册类型与项目隔离。

文档、历史记录和成员数据查询必须使用只读工具；项目记忆不得跨项目泄漏，
成员档案仅在明确允许的跨项目场景中随成员共享。
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
    """索引和查询使用同一常量向量替身，确保结果稳定命中。"""
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
        assert "发布步骤" in hits[0]["content"]  # 查询与原文用词不同，仍应语义命中。
        assert hits[0]["source_id"] == str(doc_id)

        # 项目记忆不得向其他项目泄漏。
        assert await search_project_documents(session, "怎么部署", project_id=project_b.id) == []

        # 文档检索不得混入历史记录块。
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


async def test_get_member_stats_tool(project_a: Project) -> None:
    """成员统计应保留停用标记，防止停用成员进入分配候选。"""
    from app.agents.tools import get_member_stats
    from tests.conftest import add_member
    from tests.test_memory_member_stats import _add_item

    _, alice = await add_member(project_a, "alice", "Alice123!", display_name="爱丽丝")
    _, dave = await add_member(project_a, "dave", "Dave12345!", display_name="戴夫")
    await _add_item(project_a, alice, "COMPLETED")
    await _add_item(project_a, dave, "IN_PROGRESS")
    async with async_session_factory() as session:
        from app.domains.project.models import ProjectMember as PM

        member = await session.get(PM, dave.id)
        assert member is not None
        member.is_active = False
        await session.commit()

        stats = {s["display_name"]: s for s in await get_member_stats(session, project_id=project_a.id)}
    assert stats["爱丽丝"]["completed_total"] == 1
    assert stats["爱丽丝"]["on_time_rate"] == 1.0
    assert stats["爱丽丝"]["sample_sufficient"] is False
    assert stats["戴夫"]["active_now"] == 1
    assert stats["戴夫"]["is_active"] is False


async def test_search_member_profiles_tool(
    project_a: Project, project_b: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """分配场景应能检索随成员共享的跨项目档案。"""
    from app.agents.tools import search_member_profiles
    from app.core.config import settings
    from app.domains.memory.models import MemoryChunk

    source_id = uuid.uuid4()
    # 与 embedding 替身保持同向，避免相似度波动导致用例不稳定。
    vec = [0.1] * settings.embedding_dimensions
    async with async_session_factory() as session:
        session.add(
            MemoryChunk(
                project_id=None,  # 成员档案不绑定单个项目。
                source_type="profile",
                source_id=source_id,
                content="对支付模块的历史包袱很熟",
                embedding=vec,
                model_version=settings.embedding_model,
            )
        )
        await session.commit()

        hits = await search_member_profiles(session, "支付", project_id=project_a.id)
        assert len(hits) == 1
        assert hits[0]["source_id"] == str(source_id)
        # 成员档案在授权的分配场景中可跨项目复用。
        hits = await search_member_profiles(session, "支付", project_id=project_b.id)
        assert len(hits) == 1


def test_m62_tools_registered_as_read_query() -> None:
    for name in ("get_member_stats", "search_member_profiles"):
        assert TOOL_REGISTRY[name].kind == "read_query"
