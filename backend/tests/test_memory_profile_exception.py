"""成员档案作为项目隔离例外的权限边界测试。

档案是项目级隔离的唯一例外（随人走、不挂项目），独立用例集中断言：
- 放行场景 1：leader_query——A 项目负责人在分配/查询场景命中他项目成员档案；
- 放行场景 2：agent_assignment——Agent 分配工具命中档案（服务层）；
- member_qa 不放行：普通成员检索与知识库问答均不命中档案；
- admin 只读：可读档案详情，但检索不属于放行场景（不命中档案块）。
"""

import uuid

import httpx
import pytest

from app.core.config import settings
from app.domains.memory import retriever as retriever_module
from app.domains.memory.models import MemoryChunk
from app.domains.memory.search import CALLER_AGENT_ASSIGNMENT, search_memory
from app.domains.project.models import Project
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member, auth_headers
from tests.test_file_index_pipeline import FakeEmbeddingProvider

LEADER_PW = "Leader123!"
ALICE_PW = "Alice123!"


@pytest.fixture(autouse=True)
def fake_embedding(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        retriever_module, "get_embedding_provider", lambda: FakeEmbeddingProvider()
    )


async def _setup_profile(
    client: httpx.AsyncClient, project_b: Project
) -> tuple[str, str]:
    """创建成员档案及不挂项目的对应索引块。"""
    _, leader_b = await add_member(project_b, "leaderb", "LeaderB123!", role="leader")
    bob_user, _ = await add_member(project_b, "bob", "Bob12345!", display_name="鲍勃")
    headers_b = await auth_headers(client, "leaderb", "LeaderB123!", project_id=str(project_b.id))
    content = "鲍勃对支付模块的历史包袱很熟"
    resp = await client.put(
        f"/api/v1/memory/member-profiles/{bob_user.id}",
        headers=headers_b,
        json={"content": content},
    )
    assert resp.status_code == 200, resp.text
    async with async_session_factory() as session:
        session.add(
            MemoryChunk(
                project_id=None,  # profile 随人走，不挂项目
                source_type="profile",
                source_id=uuid.uuid4(),
                content=content,
                embedding=[0.1] * settings.embedding_dimensions,
                model_version=settings.embedding_model,
            )
        )
        await session.commit()
    return str(bob_user.id), content


async def test_leader_query_allowed(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """项目负责人在分配查询中应能命中其他项目成员的档案。"""
    _, content = await _setup_profile(client, project_b)
    _, leader_a = await add_member(project_a, "leader", LEADER_PW, role="leader")
    headers_a = await auth_headers(client, "leader", LEADER_PW, project_id=str(project_a.id))

    resp = await client.post(
        "/api/v1/memory/search",
        headers=headers_a,
        json={"query": "支付", "caller": "leader_query"},
    )
    assert resp.status_code == 200, resp.text
    assert any(content in r["content"] for r in resp.json()["results"])


async def test_agent_assignment_allowed(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """内部 Agent 分配调用应能命中成员档案。"""
    _, content = await _setup_profile(client, project_b)
    async with async_session_factory() as session:
        results = await search_memory(
            session,
            member=None,
            is_admin=False,
            project_id=project_a.id,
            query="支付",
            caller=CALLER_AGENT_ASSIGNMENT,
        )
    assert any(content in r.content for r in results)


async def test_member_qa_not_allowed(
    client: httpx.AsyncClient, project_a: Project, project_b: Project
) -> None:
    """普通成员检索与知识库问答均不得命中成员档案。"""
    await _setup_profile(client, project_b)
    _, alice = await add_member(project_a, "alice", ALICE_PW)
    headers_a = await auth_headers(client, "alice", ALICE_PW, project_id=str(project_a.id))

    resp = await client.post(
        "/api/v1/memory/search", headers=headers_a, json={"query": "支付"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"] == []

    resp = await client.post(
        "/api/v1/memory/qa", headers=headers_a, json={"question": "谁懂支付"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "refused"
    assert body["clues"] == []  # 档案连线索都不命中


async def test_admin_readonly_but_not_release(
    client: httpx.AsyncClient, project_a: Project, project_b: Project, admin_user
) -> None:
    """管理员可只读查看档案详情，但普通检索不得命中档案块。"""
    user_id, content = await _setup_profile(client, project_b)
    admin_headers = await auth_headers(client, "admin", "Admin123!")
    admin_headers["X-Project-Id"] = str(project_a.id)

    resp = await client.get(
        f"/api/v1/memory/member-profiles/{user_id}", headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == content

    resp = await client.post(
        "/api/v1/memory/search", headers=admin_headers, json={"query": "支付"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"] == []
