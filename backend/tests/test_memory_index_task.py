"""记忆索引任务测试（M1.8 验收）。

- 成功路径：payload 文本经索引服务写入 memory_chunks（fake embedding provider）；
- 失败路径：异常按指数退避重入延迟队列，attempt 自增；
- 重试耗尽：attempt 达到 MAX_ATTEMPTS 后丢弃，不再入队。
"""

import json
import uuid

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.domains.memory import indexer as indexer_module
from app.domains.memory.models import MemoryChunk
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.models.embedding import EmbeddingProvider
from app.infrastructure.queue.queue import DELAYED_QUEUE_KEY
from app.workers.memory_index import MAX_ATTEMPTS, execute_memory_index


class FakeEmbeddingProvider(EmbeddingProvider):
    name = "fake"
    model = "fake-embedding:v1"
    dimensions = settings.embedding_dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * self.dimensions for _ in texts]


@pytest.fixture
async def redis_client():
    client = create_redis_client()
    await client.delete(DELAYED_QUEUE_KEY)
    yield client
    await client.delete(DELAYED_QUEUE_KEY)
    await client.aclose()


def _payload(source_id: uuid.UUID, project_id: uuid.UUID | None, **extra) -> dict:
    return {
        "project_id": str(project_id) if project_id else None,
        "source_type": "document",
        "source_id": str(source_id),
        "text": "第一段内容。\n\n" + "第二段内容。" * 200,
        **extra,
    }


async def test_index_task_success(redis_client, project_a, monkeypatch: pytest.MonkeyPatch) -> None:
    """成功：文本切块写入 memory_chunks，不进延迟队列。"""
    monkeypatch.setattr(
        indexer_module, "get_embedding_provider", lambda: FakeEmbeddingProvider()
    )
    source_id = uuid.uuid4()

    await execute_memory_index(_payload(source_id, project_a.id), redis_client)

    async with async_session_factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(MemoryChunk)
                .where(MemoryChunk.source_id == source_id)
            )
        ).scalar_one()
        version = (
            await session.execute(
                select(MemoryChunk.model_version)
                .where(MemoryChunk.source_id == source_id)
                .limit(1)
            )
        ).scalar_one()
    assert count > 1
    assert version == settings.embedding_model
    assert await redis_client.zcard(DELAYED_QUEUE_KEY) == 0


async def test_index_task_failure_retries_with_backoff(
    redis_client, project_a, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败：重入延迟队列，attempt+1，退避基数 10s。"""

    async def _boom(self, **kwargs):  # noqa: ANN001, ANN003
        raise RuntimeError("embedding service down")

    monkeypatch.setattr(indexer_module.MemoryIndexService, "rebuild_chunks", _boom)
    source_id = uuid.uuid4()

    await execute_memory_index(_payload(source_id, project_a.id, attempt=0), redis_client)

    entries = await redis_client.zrange(DELAYED_QUEUE_KEY, 0, -1)
    assert len(entries) == 1
    retried = json.loads(entries[0])
    assert retried["type"] == "memory.index"
    assert retried["payload"]["attempt"] == 1
    assert retried["payload"]["source_id"] == str(source_id)


async def test_index_task_gives_up_after_max_attempts(
    redis_client, project_a, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重试耗尽：不再入队，任务丢弃（只记日志）。"""

    async def _boom(self, **kwargs):  # noqa: ANN001, ANN003
        raise RuntimeError("still down")

    monkeypatch.setattr(indexer_module.MemoryIndexService, "rebuild_chunks", _boom)

    await execute_memory_index(
        _payload(uuid.uuid4(), project_a.id, attempt=MAX_ATTEMPTS), redis_client
    )

    assert await redis_client.zcard(DELAYED_QUEUE_KEY) == 0
