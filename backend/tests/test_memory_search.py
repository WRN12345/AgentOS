"""检索权限层测试（M2.9 验收，设计文档第 12 节）。

- 项目在职成员可检索本项目；跨项目/停用成员 → 404（不暴露存在性）；
- 全局 admin 只读可查任意项目（不依赖成员身份）；
- 调用方标识校验（档案跨项目放行的判定依据，16.12）。
"""

import uuid

import pytest

from app.core.config import settings
from app.core.errors import ApiException
from app.domains.memory.models import MemoryChunk
from app.domains.memory.search import CALLER_MEMBER_QA, search_memory
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member


class _FakeProvider:
    name = "fake"
    model = settings.embedding_model
    dimensions = settings.embedding_dimensions

    async def embed(self, texts):
        vec = [0.0] * self.dimensions
        vec[0] = 1.0
        return [vec for _ in texts]


async def _seed_chunk(project_id) -> None:
    vec = [0.0] * settings.embedding_dimensions
    vec[0] = 1.0
    async with async_session_factory() as session:
        session.add(
            MemoryChunk(
                project_id=project_id,
                source_type="document",
                source_id=uuid.uuid4(),
                content="项目内文档",
                embedding=vec,
                model_version=settings.embedding_model,
            )
        )
        await session.commit()


async def _search(member, is_admin, project_id, monkeypatch):
    from app.domains.memory import retriever as retriever_module

    monkeypatch.setattr(
        retriever_module, "get_embedding_provider", lambda: _FakeProvider()
    )
    async with async_session_factory() as session:
        return await search_memory(
            session,
            member=member,
            is_admin=is_admin,
            project_id=project_id,
            query="文档",
            caller=CALLER_MEMBER_QA,
        )


async def test_member_searches_own_project(project_a, monkeypatch) -> None:
    await _seed_chunk(project_a.id)
    _, member = await add_member(project_a, "alice", "Alice123!")

    results = await _search(member, False, project_a.id, monkeypatch)

    assert [r.content for r in results] == ["项目内文档"]


async def test_member_cross_project_404(project_a, project_b, monkeypatch) -> None:
    _, member = await add_member(project_a, "alice", "Alice123!")

    with pytest.raises(ApiException) as exc_info:
        await _search(member, False, project_b.id, monkeypatch)
    assert exc_info.value.status_code == 404


async def test_inactive_member_404(project_a, monkeypatch) -> None:
    _, member = await add_member(project_a, "alice", "Alice123!")
    member.is_active = False

    with pytest.raises(ApiException) as exc_info:
        await _search(member, False, project_a.id, monkeypatch)
    assert exc_info.value.status_code == 404


async def test_global_admin_readonly_any_project(project_a, monkeypatch) -> None:
    await _seed_chunk(project_a.id)

    results = await _search(None, True, project_a.id, monkeypatch)

    assert [r.content for r in results] == ["项目内文档"]


async def test_non_member_non_admin_404(project_a, monkeypatch) -> None:
    with pytest.raises(ApiException) as exc_info:
        await _search(None, False, project_a.id, monkeypatch)
    assert exc_info.value.status_code == 404


async def test_unknown_caller_rejected(project_a) -> None:
    _, member = await add_member(project_a, "alice", "Alice123!")
    async with async_session_factory() as session:
        with pytest.raises(ValueError):
            await search_memory(
                session,
                member=member,
                is_admin=False,
                project_id=project_a.id,
                query="x",
                caller="someone",
            )


async def test_three_caller_types_accepted_at_service_layer(project_a, monkeypatch) -> None:
    """M3.8：leader_query / agent_assignment / member_qa 三种标识均可传入并在服务层可判。

    - member_qa / leader_query：HTTP 路径（成员/负责人身份）；
    - agent_assignment：Agent 内部调用（无成员身份，信任锚为 run 的项目归属）。
    """
    from app.domains.memory.search import (
        CALLER_AGENT_ASSIGNMENT,
        CALLER_LEADER_QUERY,
        CALLER_MEMBER_QA,
    )

    await _seed_chunk(project_a.id)
    _, member = await add_member(project_a, "alice", "Alice123!")

    from app.domains.memory import retriever as retriever_module

    monkeypatch.setattr(
        retriever_module, "get_embedding_provider", lambda: _FakeProvider()
    )
    async with async_session_factory() as session:
        for caller, m in (
            (CALLER_MEMBER_QA, member),
            (CALLER_LEADER_QUERY, member),
            (CALLER_AGENT_ASSIGNMENT, None),  # Agent 内部调用无成员身份
        ):
            results = await search_memory(
                session,
                member=m,
                is_admin=False,
                project_id=project_a.id,
                query="文档",
                caller=caller,
            )
            assert [r.content for r in results] == ["项目内文档"], caller
