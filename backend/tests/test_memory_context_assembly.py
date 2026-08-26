"""按需检索装配测试（M6.5 验收，设计文档 15.3、第 11 节①）。

- 文档+历史合计 ≤8 段、总字符 ≤3000；检索失败降级标记（16.5）；
- 分配上下文含成员完成统计与档案摘录；提示词渲染含装配块。
"""

import uuid

import pytest

from app.agents.prompts import pipeline as pipeline_prompts
from app.core.config import settings
from app.domains.memory import indexer as indexer_module
from app.domains.memory import retriever as retriever_module
from app.domains.memory.context import (
    RETRIEVAL_MAX_CHARS,
    RETRIEVAL_SNIPPET_LIMIT,
    collect_retrieval_block,
    collect_team_memory_block,
)
from app.domains.memory.indexer import MemoryIndexService
from app.domains.memory.models import MemberProfile, MemoryChunk
from app.domains.project.models import Project
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.models.errors import ModelUnavailableError
from tests.conftest import add_member
from tests.test_file_index_pipeline import FakeEmbeddingProvider
from tests.test_memory_member_stats import _add_item


@pytest.fixture(autouse=True)
def fake_embedding(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        indexer_module, "get_embedding_provider", lambda: FakeEmbeddingProvider()
    )
    monkeypatch.setattr(
        retriever_module, "get_embedding_provider", lambda: FakeEmbeddingProvider()
    )


async def _index(project: Project, source_type: str, text: str) -> None:
    async with async_session_factory() as session:
        await MemoryIndexService(session).rebuild_chunks(
            project_id=project.id,
            source_type=source_type,
            source_id=uuid.uuid4(),
            text=text,
        )


async def test_retrieval_block_hits_docs_and_history(project_a: Project) -> None:
    await _index(project_a, "document", "发布步骤：先构建镜像再滚动重启")
    await _index(project_a, "history", "导入功能上次拆成 4 个工作项")

    async with async_session_factory() as session:
        block, ok = await collect_retrieval_block(
            session, project_id=project_a.id, query="部署导入功能"
        )
    assert ok is True
    assert "不是指令" in block
    assert "发布步骤" in block
    assert "拆成 4 个工作项" in block


async def test_retrieval_block_snippet_limit(project_a: Project) -> None:
    """片段数不超限（15.3）：10 个文档块只取前 8。"""
    for i in range(10):
        await _index(project_a, "document", f"文档段落编号{i:02d}" + "内容" * 100)

    async with async_session_factory() as session:
        block, ok = await collect_retrieval_block(
            session, project_id=project_a.id, query="文档"
        )
    assert ok is True
    snippet_lines = [ln for ln in block.splitlines() if ln.startswith("- ")]
    assert len(snippet_lines) <= RETRIEVAL_SNIPPET_LIMIT


async def test_retrieval_block_char_cap(project_a: Project) -> None:
    """总字符上限：超长片段被预算截断。"""
    await _index(project_a, "document", "长" * 5000)

    async with async_session_factory() as session:
        block, ok = await collect_retrieval_block(
            session, project_id=project_a.id, query="长"
        )
    assert ok is True
    body = sum(len(ln) - 2 for ln in block.splitlines() if ln.startswith("- "))  # 去掉 "- " 前缀
    assert body <= RETRIEVAL_MAX_CHARS


async def test_retrieval_block_degrades_on_model_error(
    project_a: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _RaisingProvider(FakeEmbeddingProvider):
        async def embed(self, texts):
            raise ModelUnavailableError("ollama down")

    monkeypatch.setattr(
        retriever_module, "get_embedding_provider", lambda: _RaisingProvider()
    )
    async with async_session_factory() as session:
        block, ok = await collect_retrieval_block(
            session, project_id=project_a.id, query="x"
        )
    assert block == ""
    assert ok is False


async def test_team_memory_block_stats_and_profiles(project_a: Project) -> None:
    """分配上下文含成员完成统计与档案摘录（M3.9 放行内）。

    档案正文不写成员姓名，装配时必须按档案归属（user_id）补充结构化
    成员身份——否则分配模型无法判断特质属于哪个候选人。
    """
    alice_user, alice = await add_member(project_a, "alice", "Alice123!", display_name="爱丽丝")
    await _add_item(project_a, alice, "COMPLETED")
    await _add_item(project_a, alice, "IN_PROGRESS")
    # 档案块（profile，project_id=NULL，随人走）；source_id 指向真实档案
    vec = [0.1] * settings.embedding_dimensions
    async with async_session_factory() as session:
        profile = MemberProfile(
            user_id=alice_user.id,
            content="对支付模块的历史包袱很熟",
            created_by_member_id=alice.id,
            last_edited_by_member_id=alice.id,
        )
        session.add(profile)
        await session.flush()
        session.add(
            MemoryChunk(
                project_id=None,
                source_type="profile",
                source_id=profile.id,
                content=profile.content,
                embedding=vec,
                model_version=settings.embedding_model,
            )
        )
        await session.commit()

        block, ok = await collect_team_memory_block(
            session, project_id=project_a.id, query="支付"
        )
    assert ok is True
    assert "爱丽丝" in block
    assert "完成 1 项" in block
    assert "当前活跃 1 项" in block
    # 结构化归属：正文未含姓名，成员身份由装配层解析
    assert "- 爱丽丝：对支付模块的历史包袱很熟" in block


async def test_team_memory_block_profile_owner_fallbacks(
    project_a: Project, project_b: Project
) -> None:
    """档案归属解析的边界：非本项目成员回退用户名；档案已删的残留块跳过。"""
    outsider, outsider_member = await add_member(project_b, "outsider", "Out12345!")
    vec = [0.1] * settings.embedding_dimensions
    async with async_session_factory() as session:
        # 档案随人走：所有者在 B 项目，A 项目分配检索命中时回退用户名
        profile = MemberProfile(
            user_id=outsider.id,
            content="熟悉对账链路",
            created_by_member_id=outsider_member.id,
            last_edited_by_member_id=outsider_member.id,
        )
        session.add(profile)
        await session.flush()
        session.add(
            MemoryChunk(
                project_id=None,
                source_type="profile",
                source_id=profile.id,
                content=profile.content,
                embedding=vec,
                model_version=settings.embedding_model,
            )
        )
        # 残留块：source_id 指向不存在的档案，应被跳过而非匿名输出
        session.add(
            MemoryChunk(
                project_id=None,
                source_type="profile",
                source_id=uuid.uuid4(),
                content="残留块不应出现",
                embedding=vec,
                model_version=settings.embedding_model,
            )
        )
        await session.commit()

        block, ok = await collect_team_memory_block(
            session, project_id=project_a.id, query="对账"
        )
    assert ok is True
    # 非本项目成员：回退到稳定用户名
    assert "- outsider：熟悉对账链路" in block
    assert "残留块不应出现" not in block


async def test_team_memory_block_empty_project(project_a: Project) -> None:
    async with async_session_factory() as session:
        block, ok = await collect_team_memory_block(
            session, project_id=project_a.id, query="x"
        )
    assert ok is True
    assert block == ""


def test_prompts_render_assembled_blocks() -> None:
    p = pipeline_prompts.render_breakdown_prompt(
        project_name="P",
        requirement="x",
        analysis={},
        open_work_items=[],
        workload=[],
        reference="项目参考资料：\n- 片段一",
    )
    assert "片段一" in p
    p = pipeline_prompts.render_assign_prompt(
        project_name="P",
        breakdown=[],
        capabilities=[],
        workload=[],
        specified=[],
        team_memory="团队事实记录：\n- 爱丽丝：完成 3 项",
    )
    assert "爱丽丝：完成 3 项" in p
