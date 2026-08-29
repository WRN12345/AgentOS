"""检索 API 的访问权限与调用方契约测试。

- 项目成员检索本项目 → 命中；非项目成员 → 403；
- 全局 admin 只读可查任意项目；
- leader_query 调用方标识仅负责人可用；
- 未登录 401。
"""

import uuid

import httpx
import pytest

from app.core.config import settings
from app.domains.memory import retriever as retriever_module
from app.domains.memory.models import MemoryChunk
from app.domains.project.models import Project
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member, auth_headers, create_admin_user

ALICE_PW = "Alice123!"
LEADER_PW = "Leader123!"


class _FakeProvider:
    name = "fake"
    model = settings.embedding_model
    dimensions = settings.embedding_dimensions

    async def embed(self, texts):
        vec = [0.0] * self.dimensions
        vec[0] = 1.0
        return [vec for _ in texts]


@pytest.fixture(autouse=True)
def _fake_embedding(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(retriever_module, "get_embedding_provider", lambda: _FakeProvider())


async def _seed_chunk(project_id, content: str = "部署流程文档") -> None:
    vec = [0.0] * settings.embedding_dimensions
    vec[0] = 1.0
    async with async_session_factory() as session:
        session.add(
            MemoryChunk(
                project_id=project_id,
                source_type="document",
                source_id=uuid.uuid4(),
                content=content,
                embedding=vec,
                model_version=settings.embedding_model,
            )
        )
        await session.commit()


def _search_payload(**kwargs) -> dict:
    return {"query": "部署", **kwargs}


async def test_member_search_hits(client: httpx.AsyncClient, project: Project) -> None:
    await _seed_chunk(project.id)
    _, alice = await add_member(project, "alice", ALICE_PW)
    headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id))

    resp = await client.post("/api/v1/memory/search", json=_search_payload(), headers=headers)

    assert resp.status_code == 200, resp.text
    hits = resp.json()["results"]
    assert len(hits) == 1
    assert hits[0]["content"] == "部署流程文档"
    assert hits[0]["source_type"] == "document"


async def test_non_member_403(client: httpx.AsyncClient, project_a: Project, project_b: Project) -> None:
    _, alice = await add_member(project_a, "alice", ALICE_PW)
    resp = await client.post(
        "/api/v1/memory/search",
        json=_search_payload(),
        headers=await auth_headers(client, "alice", ALICE_PW, project_id=str(project_b.id)),
    )
    assert resp.status_code == 403


async def test_admin_readonly_any_project(client: httpx.AsyncClient, project: Project) -> None:
    await _seed_chunk(project.id)
    await create_admin_user("admin", "Admin123!")
    headers = await auth_headers(client, "admin", "Admin123!", project_id=str(project.id))

    resp = await client.post("/api/v1/memory/search", json=_search_payload(), headers=headers)

    assert resp.status_code == 200, resp.text
    assert len(resp.json()["results"]) == 1


async def test_leader_query_requires_leader(client: httpx.AsyncClient, project: Project) -> None:
    _, alice = await add_member(project, "alice", ALICE_PW)
    headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id))

    resp = await client.post(
        "/api/v1/memory/search",
        json=_search_payload(caller="leader_query"),
        headers=headers,
    )
    assert resp.status_code == 403

    _, leader = await add_member(project, "leader", LEADER_PW, role="leader")
    leader_headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id))
    resp = await client.post(
        "/api/v1/memory/search",
        json=_search_payload(caller="leader_query"),
        headers=leader_headers,
    )
    assert resp.status_code == 200, resp.text


async def test_agent_assignment_caller_rejected_over_http(
    client: httpx.AsyncClient, project: Project
) -> None:
    _, leader = await add_member(project, "leader", LEADER_PW, role="leader")
    headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id))

    resp = await client.post(
        "/api/v1/memory/search",
        json=_search_payload(caller="agent_assignment"),
        headers=headers,
    )
    assert resp.status_code == 403


async def test_unauthenticated_401(client: httpx.AsyncClient, project: Project) -> None:
    resp = await client.post(
        "/api/v1/memory/search",
        json=_search_payload(),
        headers={"X-Project-Id": str(project.id)},
    )
    assert resp.status_code == 401
