"""档案入索引测试（M3.7 验收，设计文档第 7 节②）。

- 档案创建/变更自动投递 memory.index（source_type=profile，project_id=NULL）；
- worker 纯文本路径切块入库；编辑后整体重建（旧块被替换）；
- profile 块 project_id 为 NULL（随人走，16.12）。
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
    assert payload["project_id"] is None  # 随人走，不挂项目
    assert payload["text"] == "对支付模块的历史包袱很熟"


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
        assert all(c.project_id is None for c in chunks)  # profile 不挂项目
        assert any("支付模块" in c.content for c in chunks)

    # 编辑后整体重建：旧内容块被替换
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
