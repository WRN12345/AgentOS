"""验证 Agent 主动提议记忆的生成、整合与写入护栏。

值得沉淀的经验应生成待确认提议；容量接近上限时应建议整合。无提议或非法输出
不得影响主建议，负责人确认前核心记忆必须保持不变。
"""

import json
import uuid

import pytest
from sqlalchemy import select

from app.agents.models import AgentSuggestion
from app.domains.audit.models import AuditEvent
from app.domains.memory.core_memory import create_entry
from app.domains.memory.models import CORE_MEMORY_BUDGET_CHARS, CoreMemoryEntry
from app.domains.memory.proposals import MEMORY_PROPOSAL_TYPE
from app.domains.project.models import Project, ProjectMember
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from tests.test_agent_pipeline import (
    _analysis_stage,
    _assign_stage,
    _breakdown_stage,
    _patch_provider,
    _run_once,
    _ScriptedProvider,
    _trigger,
)
from tests.conftest import add_member


@pytest.fixture
async def redis_client():
    client = create_redis_client()
    yield client
    await client.aclose()


def _memory_stage(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


async def _run_pipeline_with_memory_stage(
    client, project: Project, monkeypatch: pytest.MonkeyPatch, memory_payload: dict
) -> None:
    # 使用唯一用户名，避免同一用例多次运行触发用户名唯一约束。
    suffix = uuid.uuid4().hex[:8]
    _, zhangsan = await add_member(
        project, f"zhangsan-{suffix}", "Zhang123!", display_name="张三"
    )
    provider = _ScriptedProvider(
        [
            _analysis_stage(),
            _breakdown_stage(),
            _assign_stage(zhangsan, zhangsan),
            _memory_stage(memory_payload),
        ]
    )
    _patch_provider(monkeypatch, provider)
    run = await _trigger(client, "搭建 RAG 问答平台", project_id=project.id)
    await _run_once(client, run.id, "搭建 RAG 问答平台")


async def _memory_proposals() -> list[AgentSuggestion]:
    async with async_session_factory() as session:
        return list(
            (
                await session.execute(
                    select(AgentSuggestion).where(
                        AgentSuggestion.suggestion_type == MEMORY_PROPOSAL_TYPE
                    )
                )
            ).scalars().all()
        )


async def test_agent_proposes_memory_when_worth_remembering(
    project: Project, leader: ProjectMember, redis_client, monkeypatch
) -> None:
    await _run_pipeline_with_memory_stage(
        redis_client,
        project,
        monkeypatch,
        {
            "action": "create",
            "content": "RAG 类需求评估集先行，接口后置",
            "entry_ids": [],
            "reason": "本次拆解验证的顺序可复用",
        },
    )

    proposals = await _memory_proposals()
    assert len(proposals) == 1
    assert proposals[0].review_status == "pending"
    assert proposals[0].content["action"] == "create"
    assert proposals[0].content["content"] == "RAG 类需求评估集先行，接口后置"

    # 提议可审计，但负责人确认前不得写入核心记忆。
    async with async_session_factory() as session:
        assert list((await session.execute(select(CoreMemoryEntry))).scalars().all()) == []
        actions = list(
            (await session.execute(select(AuditEvent.action))).scalars().all()
        )
    assert "core_memory.proposed" in actions
    assert "core_memory.created" not in actions


async def test_agent_proposes_consolidation_when_nearly_full(
    project: Project, leader: ProjectMember, redis_client, monkeypatch
) -> None:
    async with async_session_factory() as session:
        e1 = await create_entry(session, leader, content="x" * 2000)
        e2 = await create_entry(session, leader, content="y" * 2000)  # 将容量推至整合阈值以上。

    await _run_pipeline_with_memory_stage(
        redis_client,
        project,
        monkeypatch,
        {
            "action": "consolidate",
            "content": "合并后的精简约定",
            "entry_ids": [str(e1.id), str(e2.id)],
            "reason": "容量将满，两条内容重复",
        },
    )

    proposals = await _memory_proposals()
    assert len(proposals) == 1
    assert proposals[0].content["action"] == "consolidate"
    assert proposals[0].content["entry_ids"] == [str(e1.id), str(e2.id)]


async def test_no_proposal_when_none_or_invalid(
    project: Project, leader: ProjectMember, redis_client, monkeypatch
) -> None:
    await _run_pipeline_with_memory_stage(
        redis_client, project, monkeypatch, {"action": "none", "content": "", "entry_ids": []}
    )
    assert await _memory_proposals() == []

    # 无法整合少于两条记忆，非法提议应被忽略而不影响主建议。
    await _run_pipeline_with_memory_stage(
        redis_client,
        project,
        monkeypatch,
        {"action": "consolidate", "content": "合并", "entry_ids": [str(uuid.uuid4())]},
    )
    assert await _memory_proposals() == []
    async with async_session_factory() as session:
        pipeline_suggestions = list(
            (
                await session.execute(
                    select(AgentSuggestion).where(AgentSuggestion.suggestion_type == "pipeline")
                )
            ).scalars().all()
        )
    assert len(pipeline_suggestions) == 2
