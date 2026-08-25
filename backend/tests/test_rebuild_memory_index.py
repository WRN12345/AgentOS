"""全量重建索引脚本测试（16.4）。

核心回归点：脚本清空 memory_chunks 全表，必须同时重投 core_memory 来源的
索引任务，否则重建后核心记忆从检索中静默消失（直到条目被再次编辑）。
"""

import json
import sys

import pytest
from sqlalchemy import func, select

from app.domains.memory.models import CoreMemoryEntry, MemoryChunk
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.queue.queue import QUEUE_KEY
from app.scripts import rebuild_memory_index


async def _drain_queue() -> list[dict]:
    client = create_redis_client()
    try:
        raws = await client.lrange(QUEUE_KEY, 0, -1)
        await client.delete(QUEUE_KEY)
        return [json.loads(r) for r in raws]
    finally:
        await client.aclose()


async def _run_script(*extra_args: str) -> None:
    argv = sys.argv
    sys.argv = ["rebuild_memory_index", *extra_args]
    try:
        await rebuild_memory_index.main()
    finally:
        sys.argv = argv


@pytest.mark.asyncio
async def test_rebuild_enqueues_active_core_memory_entries(project_a, leader) -> None:
    async with async_session_factory() as session:
        active = CoreMemoryEntry(
            project_id=project_a.id,
            content="约定：接口返回 camelCase",
            status="active",
            confirmed_by_member_id=leader.id,
        )
        deprecated = CoreMemoryEntry(
            project_id=project_a.id,
            content="旧约定",
            status="deprecated",
            confirmed_by_member_id=leader.id,
        )
        session.add_all([active, deprecated])
        await session.flush()
        chunk = MemoryChunk(
            project_id=project_a.id,
            source_type="core_memory",
            source_id=active.id,
            content="旧模型向量块",
            embedding=[0.0] * 1024,
            model_version="old-model",
        )
        session.add(chunk)
        await session.commit()
        active_id = active.id

    await _run_script("--yes")

    tasks = await _drain_queue()
    core_tasks = [t for t in tasks if t["payload"].get("source_type") == "core_memory"]
    # active 条目被重投；deprecated 条目不重投（worker 会按当前状态作废旧块，无需任务）
    assert [t["payload"]["source_id"] for t in core_tasks] == [str(active_id)]
    assert core_tasks[0]["payload"]["project_id"] == str(project_a.id)

    # 旧向量块已清空
    async with async_session_factory() as session:
        remaining = await session.scalar(select(func.count()).select_from(MemoryChunk))
    assert remaining == 0


@pytest.mark.asyncio
async def test_preview_mode_leaves_chunks_and_queue_untouched(project_a, leader) -> None:
    async with async_session_factory() as session:
        entry = CoreMemoryEntry(
            project_id=project_a.id,
            content="约定",
            status="active",
            confirmed_by_member_id=leader.id,
        )
        session.add(entry)
        await session.flush()
        chunk = MemoryChunk(
            project_id=project_a.id,
            source_type="core_memory",
            source_id=entry.id,
            content="向量块",
            embedding=[0.0] * 1024,
            model_version="m",
        )
        session.add(chunk)
        await session.commit()

    await _run_script()  # 不加 --yes：只预览

    async with async_session_factory() as session:
        remaining = await session.scalar(select(func.count()).select_from(MemoryChunk))
    assert remaining == 1
    assert await _drain_queue() == []
