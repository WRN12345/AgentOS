"""索引写入服务测试（M1.7 验收）。

- rebuild_chunks：文本 → 切块 → fake embedding → memory_chunks 落库，
  model_version 记录当前 EMBEDDING_MODEL（16.4）；
- 重复重建先删旧块（内容变更后索引一致）；
- profile 类型 project_id 为空（16.12 例外）；其余类型缺 project_id 报错；
- 空文本只删旧块不写新块。
"""

import uuid

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.domains.memory.indexer import MemoryIndexService
from app.domains.memory.models import MemoryChunk
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.models.embedding import EmbeddingProvider


class FakeEmbeddingProvider(EmbeddingProvider):
    name = "fake"
    model = "fake-embedding:v1"
    dimensions = settings.embedding_dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * self.dimensions for _ in texts]


async def _chunk_count(source_id: uuid.UUID) -> int:
    async with async_session_factory() as session:
        result = await session.execute(
            select(func.count()).select_from(MemoryChunk).where(MemoryChunk.source_id == source_id)
        )
        return int(result.scalar_one())


async def test_rebuild_chunks_writes_with_model_version(project_a) -> None:
    source_id = uuid.uuid4()
    text = "第一段内容。\n\n" + "第二段内容，比较长。" * 100

    async with async_session_factory() as session:
        service = MemoryIndexService(session, provider=FakeEmbeddingProvider())
        written = await service.rebuild_chunks(
            project_id=project_a.id,
            source_type="document",
            source_id=source_id,
            text=text,
        )

    assert written > 1
    assert await _chunk_count(source_id) == written
    async with async_session_factory() as session:
        chunk = (
            await session.execute(
                select(MemoryChunk).where(MemoryChunk.source_id == source_id).limit(1)
            )
        ).scalar_one()
    assert chunk.project_id == project_a.id
    assert chunk.model_version == settings.embedding_model
    assert chunk.is_current is True
    assert len(chunk.embedding) == settings.embedding_dimensions


async def test_rebuild_replaces_old_chunks(project_a) -> None:
    source_id = uuid.uuid4()
    async with async_session_factory() as session:
        service = MemoryIndexService(session, provider=FakeEmbeddingProvider())
        first = await service.rebuild_chunks(
            project_id=project_a.id, source_type="document", source_id=source_id,
            text="甲" * 800,
        )
        second = await service.rebuild_chunks(
            project_id=project_a.id, source_type="document", source_id=source_id,
            text="短内容。",
        )

    assert first > 1 and second == 1
    assert await _chunk_count(source_id) == 1


async def test_profile_type_allows_null_project() -> None:
    source_id = uuid.uuid4()
    async with async_session_factory() as session:
        service = MemoryIndexService(session, provider=FakeEmbeddingProvider())
        written = await service.rebuild_chunks(
            project_id=None, source_type="profile", source_id=source_id, text="熟悉支付模块历史。",
        )
    assert written == 1
    assert await _chunk_count(source_id) == 1


async def test_document_type_requires_project() -> None:
    async with async_session_factory() as session:
        service = MemoryIndexService(session, provider=FakeEmbeddingProvider())
        with pytest.raises(ValueError):
            await service.rebuild_chunks(
                project_id=None, source_type="document", source_id=uuid.uuid4(), text="内容",
            )


async def test_unknown_source_type_rejected(project_a) -> None:
    async with async_session_factory() as session:
        service = MemoryIndexService(session, provider=FakeEmbeddingProvider())
        with pytest.raises(ValueError):
            await service.rebuild_chunks(
                project_id=project_a.id, source_type="unknown", source_id=uuid.uuid4(), text="内容",
            )


async def test_empty_text_only_deletes(project_a) -> None:
    source_id = uuid.uuid4()
    async with async_session_factory() as session:
        service = MemoryIndexService(session, provider=FakeEmbeddingProvider())
        await service.rebuild_chunks(
            project_id=project_a.id, source_type="document", source_id=source_id, text="有内容。",
        )
        assert await _chunk_count(source_id) == 1
        written = await service.rebuild_chunks(
            project_id=project_a.id, source_type="document", source_id=source_id, text="",
        )

    assert written == 0
    assert await _chunk_count(source_id) == 0
