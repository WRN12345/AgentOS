"""索引状态机与重试接口测试（M2.4 验收，设计文档第 6 节）。

- 状态机：pending → indexing → indexed/failed 合法；failed → pending（重试）合法；
  终态（indexed/unindexed）与其他跳转一律 409；
- 重试接口：failed 文件回到 pending 并重投 memory.index 任务；非 failed 409；
  跨项目 404（不暴露存在性）；
- 恢复扫描：超时 indexing 续租重投；首次投递失败遗留的超时 pending 重新入队。
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.core.config import settings
from app.domains.files.models import StoredFile
from app.domains.files.service import (
    INDEX_FAILED,
    INDEX_INDEXED,
    INDEX_INDEXING,
    INDEX_PENDING,
    INDEX_UNINDEXED,
    transition_index_status,
)
from app.domains.project.models import Project
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.queue.queue import QUEUE_KEY
from app.infrastructure.storage.local import LocalStorageProvider
from app.infrastructure.storage.provider import get_storage_provider
from app.workers import memory_index as memory_index_module
from app.workers.memory_index import recover_stale_file_indexes
from app.main import app
from tests.conftest import add_member, auth_headers

ALICE_PW = "Alice123!"


@pytest.fixture
def storage(tmp_path: Path):
    provider = LocalStorageProvider(tmp_path)
    app.dependency_overrides[get_storage_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_storage_provider, None)


def _stored(status: str = INDEX_PENDING) -> StoredFile:
    stored = StoredFile(index_status=status)
    # transition_index_status 只读写字段，不需要入库
    return stored


def test_index_status_transitions() -> None:
    stored = _stored(INDEX_PENDING)
    transition_index_status(stored, INDEX_INDEXING)
    transition_index_status(stored, INDEX_FAILED)
    transition_index_status(stored, INDEX_PENDING)
    transition_index_status(stored, INDEX_INDEXING)
    transition_index_status(stored, INDEX_INDEXED)
    assert stored.index_status == INDEX_INDEXED


def test_index_status_terminal_states_reject() -> None:
    for terminal in (INDEX_INDEXED, INDEX_UNINDEXED):
        stored = _stored(terminal)
        with pytest.raises(Exception) as exc_info:
            transition_index_status(stored, INDEX_PENDING)
        assert "409" in str(exc_info.value.status_code)  # type: ignore[attr-defined]


def test_index_status_invalid_jump_rejected() -> None:
    stored = _stored(INDEX_PENDING)
    with pytest.raises(Exception):
        transition_index_status(stored, INDEX_INDEXED)  # 须先经 indexing


async def _upload_failed_file(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> str:
    resp = await client.post(
        "/api/v1/files",
        headers=headers,
        files={"file": ("doc.md", b"content", "text/markdown")},
    )
    assert resp.status_code == 201, resp.text
    file_id = resp.json()["id"]
    async with async_session_factory() as session:
        stored = await session.get(StoredFile, uuid.UUID(file_id))
        stored.index_status = INDEX_FAILED  # 模拟索引失败
        await session.commit()
    return file_id


async def test_retry_endpoint_failed_to_pending_and_enqueues(
    client: httpx.AsyncClient, project: Project, storage
) -> None:
    _, alice = await add_member(project, "alice", ALICE_PW)
    headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id))
    file_id = await _upload_failed_file(client, headers)

    redis_client = create_redis_client()
    await redis_client.delete(QUEUE_KEY)
    try:
        resp = await client.post(f"/api/v1/files/{file_id}/index-retry", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["index_status"] == INDEX_PENDING

        queued = await redis_client.lrange(QUEUE_KEY, 0, -1)
        assert len(queued) == 1
        task = json.loads(queued[0])
        assert task["type"] == "memory.index"
        assert task["payload"]["stored_file_id"] == file_id
        assert task["payload"]["source_type"] == "document"
    finally:
        await redis_client.delete(QUEUE_KEY)
        await redis_client.aclose()


async def test_stale_indexing_file_is_requeued(
    client: httpx.AsyncClient, project: Project, storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """worker 中断遗留的超时 indexing 文件会重新入队并续租。"""
    _, alice = await add_member(project, "alice", ALICE_PW)
    headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id))
    resp = await client.post(
        "/api/v1/files",
        headers=headers,
        files={"file": ("stale.md", b"content", "text/markdown")},
    )
    assert resp.status_code == 201, resp.text
    file_id = resp.json()["id"]
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        stored = await session.get(StoredFile, uuid.UUID(file_id))
        assert stored is not None
        transition_index_status(stored, INDEX_INDEXING)
        stored.index_started_at = now - timedelta(
            seconds=settings.file_index_lease_seconds + 1
        )
        await session.commit()

    redis_client = create_redis_client()
    await redis_client.delete(QUEUE_KEY)
    original_enqueue = memory_index_module.enqueue

    async def _enqueue_fails(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        raise RuntimeError("redis unavailable")

    try:
        monkeypatch.setattr(memory_index_module, "enqueue", _enqueue_fails)
        assert await recover_stale_file_indexes(redis_client, now=now) == 0
        async with async_session_factory() as session:
            stored = await session.get(StoredFile, uuid.UUID(file_id))
            assert stored is not None
            assert stored.index_status == INDEX_INDEXING
            assert stored.index_started_at < now

        monkeypatch.setattr(memory_index_module, "enqueue", original_enqueue)
        assert await recover_stale_file_indexes(redis_client, now=now) == 1
        async with async_session_factory() as session:
            stored = await session.get(StoredFile, uuid.UUID(file_id))
            assert stored is not None
            assert stored.index_status == INDEX_INDEXING
            assert stored.index_started_at == now
        queued = [json.loads(raw) for raw in await redis_client.lrange(QUEUE_KEY, 0, -1)]
        assert len(queued) == 1
        assert queued[0]["type"] == "memory.index"
        assert queued[0]["payload"]["stored_file_id"] == file_id
    finally:
        await redis_client.delete(QUEUE_KEY)
        await redis_client.aclose()


async def test_stale_pending_file_is_requeued(
    client: httpx.AsyncClient, project: Project, storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """首次投递失败（Redis 短暂不可用）遗留的超时 pending 文件会被恢复扫描重新入队。

    状态保持 pending（worker 尚未消费）；index_started_at 记录上次投递时间，
    下一轮扫描不会重复入队。
    """
    _, alice = await add_member(project, "alice", ALICE_PW)
    headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id))
    resp = await client.post(
        "/api/v1/files",
        headers=headers,
        files={"file": ("stale.md", b"content", "text/markdown")},
    )
    assert resp.status_code == 201, resp.text
    file_id = resp.json()["id"]
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        stored = await session.get(StoredFile, uuid.UUID(file_id))
        assert stored is not None
        assert stored.index_status == INDEX_PENDING
        assert stored.index_started_at is None
        # 模拟首次投递失败：上传时间（updated_at）已超过租约，此后无人触碰该行
        stored.updated_at = now - timedelta(seconds=settings.file_index_lease_seconds + 1)
        await session.commit()

    redis_client = create_redis_client()
    await redis_client.delete(QUEUE_KEY)
    original_enqueue = memory_index_module.enqueue

    async def _enqueue_fails(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        raise RuntimeError("redis unavailable")

    try:
        # Redis 仍不可用：不入队、不推进投递时间，等下一轮
        monkeypatch.setattr(memory_index_module, "enqueue", _enqueue_fails)
        assert await recover_stale_file_indexes(redis_client, now=now) == 0
        async with async_session_factory() as session:
            stored = await session.get(StoredFile, uuid.UUID(file_id))
            assert stored is not None
            assert stored.index_status == INDEX_PENDING
            assert stored.index_started_at is None

        # Redis 恢复：重新入队，状态仍 pending（由 worker 消费后推进）
        monkeypatch.setattr(memory_index_module, "enqueue", original_enqueue)
        assert await recover_stale_file_indexes(redis_client, now=now) == 1
        async with async_session_factory() as session:
            stored = await session.get(StoredFile, uuid.UUID(file_id))
            assert stored is not None
            assert stored.index_status == INDEX_PENDING
            assert stored.index_started_at == now
        queued = [json.loads(raw) for raw in await redis_client.lrange(QUEUE_KEY, 0, -1)]
        assert len(queued) == 1
        assert queued[0]["type"] == "memory.index"
        assert queued[0]["payload"]["stored_file_id"] == file_id

        # 同一轮 cutoff 内不重复投递
        assert await recover_stale_file_indexes(redis_client, now=now) == 0
    finally:
        await redis_client.delete(QUEUE_KEY)
        await redis_client.aclose()


async def test_retry_endpoint_rejects_non_failed(
    client: httpx.AsyncClient, project: Project, storage
) -> None:
    _, alice = await add_member(project, "alice", ALICE_PW)
    headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id))
    resp = await client.post(
        "/api/v1/files",
        headers=headers,
        files={"file": ("doc.md", b"content", "text/markdown")},
    )
    file_id = resp.json()["id"]  # 状态 pending，非 failed

    retry = await client.post(f"/api/v1/files/{file_id}/index-retry", headers=headers)
    assert retry.status_code == 409
    assert retry.json()["code"] == "FILE_INDEX_INVALID_TRANSITION"


async def test_retry_endpoint_cross_project_404(
    client: httpx.AsyncClient, project_a: Project, project_b: Project, storage
) -> None:
    _, alice = await add_member(project_a, "alice", ALICE_PW)
    headers_a = await auth_headers(client, "alice", ALICE_PW, project_id=str(project_a.id))
    file_id = await _upload_failed_file(client, headers_a)

    _, bob = await add_member(project_b, "bob", "Bob12345!")
    headers_b = await auth_headers(client, "bob", "Bob12345!", project_id=str(project_b.id))
    resp = await client.post(f"/api/v1/files/{file_id}/index-retry", headers=headers_b)
    assert resp.status_code == 404
