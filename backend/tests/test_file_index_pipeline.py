"""上传即入库与索引管道测试（M2.6 验收，设计文档第 3、6 节）。

- 上传可读取格式（.md）→ 状态 pending 且自动投递 memory.index 任务；
- 上传不支持格式（.png）→ 直接 unindexed，不投递任务；
- worker 任务全流程：读文件 → 提取 → 切块入库 → indexed；
- 扫描件 PDF → unindexed；损坏 PDF → failed（不自动重投）；
- 任务重试耗尽 → 文件标记 failed；
- 并发重复任务：同一来源只产生一套块（原子认领 + 来源级互斥 + 唯一约束）。
"""

import asyncio
import json
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.domains.files.models import StoredFile
from app.domains.memory import indexer as indexer_module
from app.domains.memory.indexer import MemoryIndexService
from app.domains.memory.models import MemoryChunk
from app.domains.project.models import Project
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.models.embedding import EmbeddingProvider
from app.infrastructure.queue.queue import DELAYED_QUEUE_KEY, QUEUE_KEY
from app.infrastructure.storage.local import LocalStorageProvider
from app.infrastructure.storage.provider import get_storage_provider
from app.main import app
from app.workers import memory_index as memory_index_module
from app.workers.memory_index import MAX_ATTEMPTS, execute_memory_index
from tests.conftest import add_member, auth_headers
from tests.test_memory_extractors import SCAN_PDF, TEXT_PDF

ALICE_PW = "Alice123!"


class FakeEmbeddingProvider(EmbeddingProvider):
    name = "fake"
    model = "fake-embedding:v1"
    dimensions = settings.embedding_dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * self.dimensions for _ in texts]


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


async def _upload(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    filename: str,
    content: bytes,
    mime: str,
) -> dict:
    resp = await client.post(
        "/api/v1/files", headers=headers, files={"file": (filename, content, mime)}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_upload_md_dispatches_index_task(
    client: httpx.AsyncClient, project: Project, storage, redis_client
) -> None:
    _, alice = await add_member(project, "alice", ALICE_PW)
    headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id))

    out = await _upload(client, headers, "guide.md", "# 指南\n\n内容。".encode(), "text/markdown")

    assert out["index_status"] == "pending"
    queued = await redis_client.lrange(QUEUE_KEY, 0, -1)
    assert len(queued) == 1
    task = json.loads(queued[0])
    assert task["type"] == "memory.index"
    assert task["payload"]["stored_file_id"] == out["id"]


async def test_upload_png_marked_unindexed_without_task(
    client: httpx.AsyncClient, project: Project, storage, redis_client
) -> None:
    _, alice = await add_member(project, "alice", ALICE_PW)
    headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id))

    out = await _upload(client, headers, "photo.png", b"\x89PNG", "image/png")

    assert out["index_status"] == "unindexed"
    assert await redis_client.llen(QUEUE_KEY) == 0


async def _run_worker_for(file_id: str, storage) -> None:
    """以 worker 视角执行索引任务（storage provider 指向测试临时目录）。"""
    original = memory_index_module.get_storage_provider
    memory_index_module.get_storage_provider = lambda: storage
    try:
        await execute_memory_index({"stored_file_id": file_id, "source_type": "document"}, None)  # type: ignore[arg-type]
    finally:
        memory_index_module.get_storage_provider = original


async def _file_status(file_id: str) -> str:
    async with async_session_factory() as session:
        stored = await session.get(StoredFile, uuid.UUID(file_id))
        return stored.index_status  # type: ignore[union-attr]


async def test_worker_indexes_md_end_to_end(
    client: httpx.AsyncClient, project: Project, storage, redis_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        indexer_module, "get_embedding_provider", lambda: FakeEmbeddingProvider()
    )
    _, alice = await add_member(project, "alice", ALICE_PW)
    headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id))
    text = "# 部署指南\n\n" + "第一步，准备环境。\n\n" * 50
    out = await _upload(client, headers, "deploy.md", text.encode(), "text/markdown")

    await _run_worker_for(out["id"], storage)

    assert await _file_status(out["id"]) == "indexed"
    async with async_session_factory() as session:
        chunks = (
            await session.execute(
                select(func.count())
                .select_from(MemoryChunk)
                .where(MemoryChunk.source_id == uuid.UUID(out["id"]))
            )
        ).scalar_one()
        project_id = (
            await session.execute(
                select(MemoryChunk.project_id)
                .where(MemoryChunk.source_id == uuid.UUID(out["id"]))
                .limit(1)
            )
        ).scalar_one()
    assert chunks > 0
    assert project_id == project.id


async def test_worker_scanned_pdf_marked_unindexed(
    client: httpx.AsyncClient, project: Project, storage, redis_client
) -> None:
    _, alice = await add_member(project, "alice", ALICE_PW)
    headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id))
    out = await _upload(client, headers, "scan.pdf", SCAN_PDF, "application/pdf")

    await _run_worker_for(out["id"], storage)

    assert await _file_status(out["id"]) == "unindexed"


async def test_worker_corrupted_pdf_marked_failed(
    client: httpx.AsyncClient, project: Project, storage, redis_client
) -> None:
    _, alice = await add_member(project, "alice", ALICE_PW)
    headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id))
    out = await _upload(client, headers, "broken.pdf", b"not a pdf", "application/pdf")

    await _run_worker_for(out["id"], storage)

    assert await _file_status(out["id"]) == "failed"


async def test_retry_exhausted_marks_file_failed(
    client: httpx.AsyncClient, project: Project, storage, redis_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """瞬态故障重试耗尽：文件从 indexing 标记为 failed（可人工重试）。"""

    async def _boom(self, **kwargs):  # noqa: ANN001, ANN003
        raise RuntimeError("embedding down")

    monkeypatch.setattr(indexer_module.MemoryIndexService, "rebuild_chunks", _boom)
    _, alice = await add_member(project, "alice", ALICE_PW)
    headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id))
    out = await _upload(client, headers, "deploy.md", "内容。".encode(), "text/markdown")

    await _run_worker_for_exhausted(out["id"], storage)

    assert await _file_status(out["id"]) == "failed"
    assert await redis_client.zcard(DELAYED_QUEUE_KEY) == 0


async def _run_worker_for_exhausted(file_id: str, storage) -> None:
    original = memory_index_module.get_storage_provider
    memory_index_module.get_storage_provider = lambda: storage
    client = create_redis_client()
    try:
        await execute_memory_index(
            {
                "stored_file_id": file_id,
                "source_type": "document",
                "attempt": MAX_ATTEMPTS,
            },
            client,
        )
    finally:
        memory_index_module.get_storage_provider = original
        await client.aclose()


# ---------- 并发重复任务防重（P2 修复回归） ----------


class _SlowEmbeddingProvider(FakeEmbeddingProvider):
    """嵌入调用加延迟，放大两个并发索引任务的交错窗口。"""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        await asyncio.sleep(0.05)
        return await super().embed(texts)


def _chunk_rows_sync_check(rows: list[MemoryChunk]) -> None:
    """同一来源只存在一套块：chunk_index 恰为 0..n-1 无重复。"""
    assert len(rows) > 0
    assert sorted(r.chunk_index for r in rows) == list(range(len(rows)))


async def test_concurrent_duplicate_worker_tasks_no_duplicate_chunks(
    client: httpx.AsyncClient,
    project: Project,
    storage,
    redis_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个 worker 并发执行同一文件的索引任务（租约重投与原任务并存）：
    原子认领保证只有一个任务写出块，文件终态 indexed，无重复块。"""
    monkeypatch.setattr(
        indexer_module, "get_embedding_provider", lambda: _SlowEmbeddingProvider()
    )
    _, alice = await add_member(project, "alice", ALICE_PW)
    headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id))
    text = "# 部署指南\n\n" + "第一步，准备环境。\n\n" * 100
    out = await _upload(client, headers, "deploy.md", text.encode(), "text/markdown")

    original = memory_index_module.get_storage_provider
    memory_index_module.get_storage_provider = lambda: storage
    payload = {"stored_file_id": out["id"], "source_type": "document"}
    try:
        await asyncio.gather(
            execute_memory_index(dict(payload), None),  # type: ignore[arg-type]
            execute_memory_index(dict(payload), None),  # type: ignore[arg-type]
        )
    finally:
        memory_index_module.get_storage_provider = original

    assert await _file_status(out["id"]) == "indexed"
    async with async_session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(MemoryChunk).where(
                        MemoryChunk.source_id == uuid.UUID(out["id"])
                    )
                )
            )
            .scalars()
            .all()
        )
    _chunk_rows_sync_check(rows)


async def test_concurrent_rebuild_chunks_same_source_serialized() -> None:
    """无文件状态机保护的来源（profile 等纯文本路径）：两个并发 rebuild
    被来源级 advisory 锁串行，同一来源只保留一套块。"""
    source_id = uuid.uuid4()
    text = "并发索引内容，语义不同。\n\n" * 100  # 多块，验证块序号唯一

    async def _rebuild() -> int:
        async with async_session_factory() as session:
            return await MemoryIndexService(
                session, provider=_SlowEmbeddingProvider()
            ).rebuild_chunks(
                project_id=None,
                source_type="profile",
                source_id=source_id,
                text=text,
            )

    written = await asyncio.gather(_rebuild(), _rebuild())
    assert written[0] == written[1] > 1  # 同一份文本切出多块

    async with async_session_factory() as session:
        rows = list(
            (
                await session.execute(
                    select(MemoryChunk).where(MemoryChunk.source_id == source_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == written[0]  # 只有一套块，而非两套
    _chunk_rows_sync_check(rows)
