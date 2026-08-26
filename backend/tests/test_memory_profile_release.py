"""档案跨项目放行规则测试（M3.9 验收，设计文档 16.12）。

- leader_query / agent_assignment：命中 project_id=NULL 的 profile 块
  （A 项目负责人在分配场景可检索到 B 项目成员档案）；
- member_qa：不命中任何档案（普通成员问答不命中他项目无关人员档案），
  显式指定 source_types=["profile"] 也只返回空；
- 项目内文档检索不受放行规则影响。
"""

import uuid

from app.core.config import settings
from app.domains.memory.models import MemoryChunk
from app.domains.memory.search import (
    CALLER_AGENT_ASSIGNMENT,
    CALLER_LEADER_QUERY,
    CALLER_MEMBER_QA,
    search_memory,
)
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member
from tests.test_memory_search import _FakeProvider, _seed_chunk


async def _seed_profile_chunk() -> uuid.UUID:
    """造一条 profile 块（project_id=NULL，随人走）。"""
    source_id = uuid.uuid4()
    vec = [0.0] * settings.embedding_dimensions
    vec[0] = 1.0
    async with async_session_factory() as session:
        session.add(
            MemoryChunk(
                project_id=None,
                source_type="profile",
                source_id=source_id,
                content="对支付模块的历史包袱很熟",
                embedding=vec,
                model_version=settings.embedding_model,
            )
        )
        await session.commit()
    return source_id


async def _search(project_id, caller, member, monkeypatch, source_types=None):
    from app.domains.memory import retriever as retriever_module

    monkeypatch.setattr(
        retriever_module, "get_embedding_provider", lambda: _FakeProvider()
    )
    async with async_session_factory() as session:
        return await search_memory(
            session,
            member=member,
            is_admin=False,
            project_id=project_id,
            query="支付",
            caller=caller,
            source_types=source_types,
        )


async def test_leader_query_hits_cross_project_profile(
    project_a, project_b, monkeypatch
) -> None:
    """A 项目负责人在分配场景（leader_query）检索到 B 项目成员档案。"""
    await _seed_profile_chunk()
    _, leader = await add_member(project_a, "leader", "Leader123!", role="leader")

    results = await _search(project_a.id, CALLER_LEADER_QUERY, leader, monkeypatch)
    assert any("支付模块" in r.content for r in results)


async def test_agent_assignment_hits_profile(project_a, monkeypatch) -> None:
    """Agent 分配环节（agent_assignment）命中档案。"""
    await _seed_profile_chunk()

    results = await _search(project_a.id, CALLER_AGENT_ASSIGNMENT, None, monkeypatch)
    assert any("支付模块" in r.content for r in results)


async def test_member_qa_never_hits_profile(project_a, project_b, monkeypatch) -> None:
    """普通成员问答不命中档案——即使是本项目成员的档案（放行仅限两场景）。"""
    await _seed_profile_chunk()
    await _seed_chunk(project_a.id)  # 项目内文档对照
    _, member = await add_member(project_a, "alice", "Alice123!")

    results = await _search(project_a.id, CALLER_MEMBER_QA, member, monkeypatch)
    assert [r.content for r in results] == ["项目内文档"]

    # 显式指定 profile 类型也只返回空
    results = await _search(
        project_a.id, CALLER_MEMBER_QA, member, monkeypatch, source_types=["profile"]
    )
    assert results == []


async def test_profile_release_does_not_leak_other_project_docs(
    project_a, project_b, monkeypatch
) -> None:
    """放行只针对 profile：leader_query 依然检索不到他项目的文档/历史块。"""
    await _seed_chunk(project_b.id)  # B 项目文档
    _, leader = await add_member(project_a, "leader", "Leader123!", role="leader")

    results = await _search(project_a.id, CALLER_LEADER_QUERY, leader, monkeypatch)
    assert results == []
