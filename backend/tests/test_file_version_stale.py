"""旧版本 chunks 失效测试（M2.7 验收，设计文档第 3 节）。

- 正常时序：v1 索引完成 → 上传 v2 → v1 的块 is_current=False，v2 索引后块为 current；
- 竞态时序：v1 未索引时上传 v2 → v1 后完成索引，其块写入即失效；
- 旧块保留在库（人工追溯），只是不参与检索。
"""

import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.domains.memory import indexer as indexer_module
from app.domains.memory.models import MemoryChunk
from app.domains.project.models import Project
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.models.embedding import EmbeddingProvider
from app.infrastructure.storage.local import LocalStorageProvider
from app.infrastructure.storage.provider import get_storage_provider
from app.main import app
from app.workers import memory_index as memory_index_module
from app.workers.memory_index import execute_memory_index
from tests.conftest import add_member, auth_headers

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


@pytest.fixture(autouse=True)
def _fake_embedding(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        indexer_module, "get_embedding_provider", lambda: FakeEmbeddingProvider()
    )


async def _upload(client: httpx.AsyncClient, headers: dict[str, str], content: str) -> str:
    resp = await client.post(
        "/api/v1/files",
        headers=headers,
        files={"file": ("guide.md", content.encode(), "text/markdown")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _run_worker(file_id: str, storage) -> None:
    original = memory_index_module.get_storage_provider
    memory_index_module.get_storage_provider = lambda: storage
    try:
        await execute_memory_index({"stored_file_id": file_id}, None)  # type: ignore[arg-type]
    finally:
        memory_index_module.get_storage_provider = original


async def _chunks_of(file_id: str) -> list[MemoryChunk]:
    async with async_session_factory() as session:
        return list(
            (
                await session.execute(
                    select(MemoryChunk).where(
                        MemoryChunk.source_id == uuid.UUID(file_id)
                    )
                )
            ).scalars()
        )


async def test_new_version_stales_old_chunks(
    client: httpx.AsyncClient, project: Project, storage
) -> None:
    _, alice = await add_member(project, "alice", ALICE_PW)
    headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id))

    v1 = await _upload(client, headers, "v1 内容，关于部署步骤。")
    await _run_worker(v1, storage)
    assert all(c.is_current for c in await _chunks_of(v1))

    v2 = await _upload(client, headers, "v2 内容，发布流程已更新。")
    # 上传 v2 后：v1 的块失效但保留
    v1_chunks = await _chunks_of(v1)
    assert len(v1_chunks) > 0
    assert all(not c.is_current for c in v1_chunks)

    await _run_worker(v2, storage)
    v2_chunks = await _chunks_of(v2)
    assert len(v2_chunks) > 0
    assert all(c.is_current for c in v2_chunks)


async def test_race_late_index_of_superseded_version_writes_stale(
    client: httpx.AsyncClient, project: Project, storage
) -> None:
    """v1 的索引任务在 v2 上传后才执行：块写入即失效，不产生"旧版内容可检索"窗口。"""
    _, alice = await add_member(project, "alice", ALICE_PW)
    headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id))

    v1 = await _upload(client, headers, "v1 内容。")
    v2 = await _upload(client, headers, "v2 内容。")  # v1 尚未索引即被取代
    await _run_worker(v1, storage)

    v1_chunks = await _chunks_of(v1)
    assert len(v1_chunks) > 0
    assert all(not c.is_current for c in v1_chunks)
