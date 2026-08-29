"""记忆向量检索的排序、过滤与项目隔离测试。

- 余弦距离升序 top-k，距离上限过滤（拒答阈值）；
- 只命中 is_current 且当前模型版本的块；
- 项目严格隔离（他项目/无项目档案不混入项目内检索）；
- source_type 过滤与空查询短路。
"""

import uuid

import pytest

from app.core.config import settings
from app.domains.memory.models import MemoryChunk
from app.domains.memory.retriever import MemoryRetriever
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.models.embedding import EmbeddingProvider


def _vec(*pairs: tuple[int, float]) -> list[float]:
    """按维度下标和值构造稀疏测试向量。"""
    vec = [0.0] * settings.embedding_dimensions
    for index, value in pairs:
        vec[index] = value
    return vec


QUERY_VECTOR = _vec((0, 1.0))
NEAR_VECTOR = _vec((0, 0.5), (1, 0.5))  # 与 query 余弦距离 ≈ 0.293
FAR_VECTOR = _vec((1, 1.0))  # 与 query 余弦距离 = 1.0


class FixedEmbeddingProvider(EmbeddingProvider):
    name = "fixed"
    model = settings.embedding_model
    dimensions = settings.embedding_dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [QUERY_VECTOR for _ in texts]


async def _add_chunk(
    *,
    project_id,
    vector: list[float],
    content: str,
    source_type: str = "document",
    is_current: bool = True,
    model_version: str = settings.embedding_model,
) -> uuid.UUID:
    chunk = MemoryChunk(
        project_id=project_id,
        source_type=source_type,
        source_id=uuid.uuid4(),
        content=content,
        embedding=vector,
        model_version=model_version,
        is_current=is_current,
    )
    async with async_session_factory() as session:
        session.add(chunk)
        await session.commit()
        return chunk.id


async def _search(project, **kwargs):
    async with async_session_factory() as session:
        retriever = MemoryRetriever(session, provider=FixedEmbeddingProvider())
        return await retriever.search("查询", project_id=project.id, **kwargs)


async def test_ranking_and_threshold(project_a) -> None:
    await _add_chunk(project_id=project_a.id, vector=QUERY_VECTOR, content="完全一致")
    await _add_chunk(project_id=project_a.id, vector=NEAR_VECTOR, content="部分相似")
    await _add_chunk(project_id=project_a.id, vector=FAR_VECTOR, content="无关内容")

    results = await _search(project_a)

    assert [r.content for r in results] == ["完全一致", "部分相似"]
    assert results[0].distance < results[1].distance
    assert all(r.distance <= settings.memory_search_max_distance for r in results)


async def test_stale_and_old_model_excluded(project_a) -> None:
    await _add_chunk(project_id=project_a.id, vector=QUERY_VECTOR, content="旧版本块", is_current=False)
    await _add_chunk(
        project_id=project_a.id, vector=QUERY_VECTOR, content="旧模型块", model_version="old-model:v0"
    )
    await _add_chunk(project_id=project_a.id, vector=QUERY_VECTOR, content="有效块")

    results = await _search(project_a)

    assert [r.content for r in results] == ["有效块"]


async def test_project_isolation(project_a, project_b) -> None:
    await _add_chunk(project_id=project_b.id, vector=QUERY_VECTOR, content="B 项目内容")
    await _add_chunk(project_id=None, vector=QUERY_VECTOR, content="成员档案", source_type="profile")
    await _add_chunk(project_id=project_a.id, vector=QUERY_VECTOR, content="A 项目内容")

    results = await _search(project_a)

    assert [r.content for r in results] == ["A 项目内容"]


async def test_source_type_filter_and_limit(project_a) -> None:
    await _add_chunk(project_id=project_a.id, vector=QUERY_VECTOR, content="文档", source_type="document")
    await _add_chunk(project_id=project_a.id, vector=QUERY_VECTOR, content="历史", source_type="history")

    results = await _search(project_a, source_types=["history"])
    assert [r.content for r in results] == ["历史"]

    results = await _search(project_a, limit=1)
    assert len(results) == 1


async def test_empty_query_short_circuits(project_a) -> None:
    await _add_chunk(project_id=project_a.id, vector=QUERY_VECTOR, content="内容")
    async with async_session_factory() as session:
        retriever = MemoryRetriever(session, provider=FixedEmbeddingProvider())
        assert await retriever.search("   ", project_id=project_a.id) == []
        assert await retriever.search("", project_id=project_a.id) == []
