"""成员档案创建、编辑与延迟任务的索引一致性测试。

- 档案创建/变更自动投递 memory.index（source_type=profile，project_id=NULL）；
- worker 纯文本路径切块入库；编辑后整体重建（旧块被替换）；
- 档案索引块不挂项目并随成员流转。
"""

import json

import httpx
import pytest
from sqlalchemy import select

from app.domains.memory import indexer as indexer_module
from app.domains.memory.models import MemoryChunk, MemberProfile
from app.domains.project.models import Project
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.queue.queue import DELAYED_QUEUE_KEY, QUEUE_KEY
from app.workers.memory_index import execute_memory_index
from tests.test_file_index_pipeline import FakeEmbeddingProvider
from tests.test_memory_member_profiles import _setup_users


@pytest.fixture
async def redis_client():
    client = create_redis_client()
    await client.delete(QUEUE_KEY, DELAYED_QUEUE_KEY)
    yield client
    await client.delete(QUEUE_KEY, DELAYED_QUEUE_KEY)
    await client.aclose()


async def test_upsert_dispatches_profile_index_task(
    client: httpx.AsyncClient, project_a: Project, redis_client
) -> None:
    ctx = await _setup_users(client, project_a)
    resp = await client.put(
        f"/api/v1/memory/member-profiles/{ctx['alice_user'].id}",
        headers=ctx["leader_headers"],
        json={"content": "对支付模块的历史包袱很熟"},
    )
    assert resp.status_code == 200, resp.text

    queued = [json.loads(t) for t in await redis_client.lrange(QUEUE_KEY, 0, -1)]
    tasks = [
        t for t in queued if t["type"] == "memory.index" and t["payload"]["source_type"] == "profile"
    ]
    assert len(tasks) == 1
    payload = tasks[0]["payload"]
    assert payload["project_id"] is None
    assert payload["source_id"]
    # 任务不携带文本快照，执行时读取最新档案以避免旧任务覆盖新编辑。
    assert "text" not in payload


async def test_worker_indexes_profile_and_rebuild_on_edit(
    client: httpx.AsyncClient,
    project_a: Project,
    redis_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        indexer_module, "get_embedding_provider", lambda: FakeEmbeddingProvider()
    )
    ctx = await _setup_users(client, project_a)
    uid = str(ctx["alice_user"].id)

    async def put_and_index(content: str) -> None:
        resp = await client.put(
            f"/api/v1/memory/member-profiles/{uid}",
            headers=ctx["leader_headers"],
            json={"content": content},
        )
        assert resp.status_code == 200, resp.text
        task = json.loads((await redis_client.lrange(QUEUE_KEY, 0, -1))[0])  # LPUSH：最新在头部
        await execute_memory_index(task["payload"], None)  # type: ignore[arg-type]

    await put_and_index("对支付模块的历史包袱很熟")
    async with async_session_factory() as session:
        profile = (
            await session.execute(select(MemberProfile).where(MemberProfile.user_id == ctx["alice_user"].id))
        ).scalar_one()
        chunks = (
            await session.execute(
                select(MemoryChunk).where(
                    MemoryChunk.source_type == "profile",
                    MemoryChunk.source_id == profile.id,
                )
            )
        ).scalars().all()
        assert len(chunks) > 0
        assert all(c.project_id is None for c in chunks)
        assert any("支付模块" in c.content for c in chunks)

    await put_and_index("擅长带新人")
    async with async_session_factory() as session:
        chunks = (
            await session.execute(
                select(MemoryChunk).where(
                    MemoryChunk.source_type == "profile",
                    MemoryChunk.source_id == profile.id,
                )
            )
        ).scalars().all()
        assert all("支付模块" not in c.content for c in chunks)
        assert any("带新人" in c.content for c in chunks)


async def test_old_profile_index_task_uses_latest_content(
    client: httpx.AsyncClient,
    project_a: Project,
    redis_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """延迟重试的旧任务不得用过期文本覆盖已编辑档案。"""
    monkeypatch.setattr(
        indexer_module, "get_embedding_provider", lambda: FakeEmbeddingProvider()
    )
    ctx = await _setup_users(client, project_a)
    user_id = str(ctx["alice_user"].id)

    first = await client.put(
        f"/api/v1/memory/member-profiles/{user_id}",
        headers=ctx["leader_headers"],
        json={"content": "旧档案：熟悉支付模块"},
    )
    assert first.status_code == 200, first.text
    old_task = json.loads((await redis_client.lrange(QUEUE_KEY, 0, -1))[0])
    # 兼容旧任务携带文本快照的回归场景，处理器仍必须读取数据库最新内容。
    old_task["payload"]["text"] = "旧档案：熟悉支付模块"

    second = await client.put(
        f"/api/v1/memory/member-profiles/{user_id}",
        headers=ctx["leader_headers"],
        json={"content": "新档案：擅长带新人"},
    )
    assert second.status_code == 200, second.text
    await execute_memory_index(old_task["payload"], None)  # type: ignore[arg-type]

    async with async_session_factory() as session:
        profile = (
            await session.execute(
                select(MemberProfile).where(MemberProfile.user_id == ctx["alice_user"].id)
            )
        ).scalar_one()
        chunks = (
            await session.execute(
                select(MemoryChunk).where(
                    MemoryChunk.source_type == "profile",
                    MemoryChunk.source_id == profile.id,
                )
            )
        ).scalars().all()
    assert chunks
    assert all("旧档案" not in chunk.content for chunk in chunks)
    assert any("新档案" in chunk.content for chunk in chunks)
