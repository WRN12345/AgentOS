"""记忆模块在 HTTP API 边界上的多项目隔离测试。

双项目 fixture 下验证记忆数据隔离与越权处理：
- 检索接口：只命中本项目块（文档/历史），跨项目头 → 403；
- 核心记忆：列表不含他项目条目；他项目负责人写操作 → 404/403；
- 成员统计：严格项目内口径，跨项目数据不混入；
- 知识库问答：他项目内容不泄漏（拒答且无线索）。
"""

import uuid

import httpx
import pytest

from app.core.config import settings
from app.domains.memory import retriever as retriever_module
from app.domains.memory.models import MemoryChunk
from app.domains.project.models import Project
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member, add_member_for_existing_user, auth_headers
from tests.test_file_index_pipeline import FakeEmbeddingProvider
from tests.test_memory_member_stats import _add_item

LEADER_PW = "Leader123!"


@pytest.fixture(autouse=True)
def fake_embedding(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        retriever_module, "get_embedding_provider", lambda: FakeEmbeddingProvider()
    )


async def _seed_chunk(project_id, source_type: str, content: str) -> None:
    async with async_session_factory() as session:
        session.add(
            MemoryChunk(
                project_id=project_id,
                source_type=source_type,
                source_id=uuid.uuid4(),
                content=content,
                embedding=[0.1] * settings.embedding_dimensions,
                model_version=settings.embedding_model,
            )
        )
        await session.commit()


async def test_search_isolation_matrix(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """检索接口：A 的文档/历史块只被 A 命中；跨项目头 403；B 视角为空。"""
    await _seed_chunk(project_a.id, "document", "A 项目部署指南")
    await _seed_chunk(project_a.id, "history", "A 项目历史拆解记录")
    _, alice_a = await add_member(project_a, "alice", "Alice123!")
    _, carol_b = await add_member(project_b, "carol", "Carol123!")

    headers_a = await auth_headers(client, "alice", "Alice123!", project_id=str(project_a.id))
    resp = await client.post(
        "/api/v1/memory/search", headers=headers_a, json={"query": "部署"}
    )
    assert resp.status_code == 200, resp.text
    contents = [r["content"] for r in resp.json()["results"]]
    assert any("A 项目" in c for c in contents)
    assert all("B 项目" not in c for c in contents)

    headers_b = await auth_headers(client, "carol", "Carol123!", project_id=str(project_b.id))
    resp = await client.post(
        "/api/v1/memory/search", headers=headers_b, json={"query": "部署"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"] == []

    resp = await client.post(
        "/api/v1/memory/search",
        headers={"Authorization": headers_a["Authorization"], "X-Project-Id": str(project_b.id)},
        json={"query": "部署"},
    )
    assert resp.status_code == 403


async def test_core_memory_isolation_matrix(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """核心记忆：列表/写操作严格项目内；跨项目写 → 404，跨项目读 → 403。"""
    _, leader_a = await add_member(project_a, "leader", LEADER_PW, role="leader")
    _, leader_b = await add_member(project_b, "leaderb", "LeaderB123!", role="leader")
    headers_a = await auth_headers(client, "leader", LEADER_PW, project_id=str(project_a.id))
    headers_b = await auth_headers(client, "leaderb", "LeaderB123!", project_id=str(project_b.id))

    resp = await client.post(
        "/api/v1/memory/core-entries", headers=headers_a, json={"content": "A 项目约定"}
    )
    assert resp.status_code == 201, resp.text
    entry_id = resp.json()["id"]

    resp = await client.get("/api/v1/memory/core-entries", headers=headers_b)
    assert resp.status_code == 200, resp.text
    assert resp.json()["entries"] == []

    resp = await client.post(
        f"/api/v1/memory/core-entries/{entry_id}/deprecate", headers=headers_b
    )
    assert resp.status_code == 404

    resp = await client.get(
        "/api/v1/memory/core-entries",
        headers={"Authorization": headers_b["Authorization"], "X-Project-Id": str(project_a.id)},
    )
    assert resp.status_code == 403


async def test_member_stats_isolation_matrix(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """成员统计：严格项目内口径，同一用户跨项目数据不混入。"""
    user, member_a = await add_member(project_a, "dave", "Dave12345!", display_name="戴夫")
    member_b = await add_member_for_existing_user(
        async_session_factory, project_b, user, display_name="戴夫"
    )
    await _add_item(project_a, member_a, "COMPLETED")
    await _add_item(project_b, member_b, "COMPLETED")
    await _add_item(project_b, member_b, "COMPLETED")

    headers_a = await auth_headers(client, "dave", "Dave12345!", project_id=str(project_a.id))
    resp = await client.get("/api/v1/memory/member-stats", headers=headers_a)
    assert resp.status_code == 200, resp.text
    stats_a = {s["display_name"]: s for s in resp.json()}
    assert stats_a["戴夫"]["completed_total"] == 1

    headers_b = await auth_headers(client, "dave", "Dave12345!", project_id=str(project_b.id))
    resp = await client.get("/api/v1/memory/member-stats", headers=headers_b)
    stats_b = {s["display_name"]: s for s in resp.json()}
    assert stats_b["戴夫"]["completed_total"] == 2


async def test_qa_isolation_matrix(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """知识库问答：B 成员提问不命中 A 的内容（拒答且无线索泄漏）。"""
    await _seed_chunk(project_a.id, "document", "A 项目部署指南")
    _, carol_b = await add_member(project_b, "carol", "Carol123!")

    headers_b = await auth_headers(client, "carol", "Carol123!", project_id=str(project_b.id))
    resp = await client.post(
        "/api/v1/memory/qa", headers=headers_b, json={"question": "部署流程是什么"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "refused"
    assert body["clues"] == []
