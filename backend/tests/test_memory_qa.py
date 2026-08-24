"""问答检索与阈值拒答测试（M7.1 验收，设计文档 16.13）。

- 阈值内命中 → 可作答（answerable + hits）；
- 全部低于阈值 → 拒答（answerable=False），并返回最接近的线索供人工判断；
- member_qa 路径不命中成员档案（16.12）；
- 阈值来自 settings.memory_search_max_distance（可配置）。
"""

import uuid

import pytest

from app.core.config import settings
from app.domains.memory import retriever as retriever_module
from app.domains.memory.models import MemoryChunk
from app.domains.memory.qa import retrieve_for_qa
from app.domains.project.models import Project
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member
from tests.test_file_index_pipeline import FakeEmbeddingProvider


@pytest.fixture(autouse=True)
def fake_embedding(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        retriever_module, "get_embedding_provider", lambda: FakeEmbeddingProvider()
    )


async def _seed(project_id, content: str, *, same_direction: bool, source_type: str = "document") -> None:
    """same_direction=True 的块与查询向量同向（距离 0，必命中）；
    False 的块反向（余弦距离 2，必然低于阈值）。"""
    dims = settings.embedding_dimensions
    vec = [0.1] * dims if same_direction else [-0.1] * dims
    async with async_session_factory() as session:
        session.add(
            MemoryChunk(
                project_id=project_id,
                source_type=source_type,
                source_id=uuid.uuid4(),
                content=content,
                embedding=vec,
                model_version=settings.embedding_model,
            )
        )
        await session.commit()


async def test_answerable_when_hit_within_threshold(project_a: Project) -> None:
    _, member = await add_member(project_a, "alice", "Alice123!")
    await _seed(project_a.id, "发布步骤：先构建镜像", same_direction=True)

    async with async_session_factory() as session:
        result = await retrieve_for_qa(
            session, member=member, project_id=project_a.id, query="怎么部署"
        )
    assert result.answerable is True
    assert len(result.hits) == 1
    assert "发布步骤" in result.hits[0].content
    assert result.clues == []


async def test_refusal_with_clues_when_below_threshold(project_a: Project) -> None:
    """宁拒答不编造：低于阈值不生成回答，列出最接近的线索（16.13）。"""
    _, member = await add_member(project_a, "alice", "Alice123!")
    await _seed(project_a.id, "毫不相干的历史记录一", same_direction=False)
    await _seed(project_a.id, "毫不相干的历史记录二", same_direction=False)

    async with async_session_factory() as session:
        result = await retrieve_for_qa(
            session, member=member, project_id=project_a.id, query="部署流程是什么"
        )
    assert result.answerable is False
    assert result.hits == []
    assert len(result.clues) == 2  # 最接近的线索供人工判断
    assert all(r.distance > settings.memory_search_max_distance for r in result.clues)


async def test_refusal_when_no_memory_at_all(project_a: Project) -> None:
    _, member = await add_member(project_a, "alice", "Alice123!")
    async with async_session_factory() as session:
        result = await retrieve_for_qa(
            session, member=member, project_id=project_a.id, query="任意问题"
        )
    assert result.answerable is False
    assert result.clues == []


async def test_qa_never_hits_member_profiles(project_a: Project) -> None:
    """member_qa 不命中成员档案（16.12 放行仅限负责人查询与 Agent 分配）。"""
    _, member = await add_member(project_a, "alice", "Alice123!")
    await _seed(None, "对支付模块很熟", same_direction=True, source_type="profile")

    async with async_session_factory() as session:
        result = await retrieve_for_qa(
            session, member=member, project_id=project_a.id, query="谁懂支付"
        )
    assert result.answerable is False  # 档案不命中 → 无可作答依据
    assert result.clues == []
