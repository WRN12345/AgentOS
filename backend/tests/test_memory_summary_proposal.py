"""工作项总结生成核心记忆提议的闭环测试。

- 总结产出自动生成 memory_proposal（pending），不直接生效；
- 确认后进入核心记忆；拒绝后核心记忆不变；
- 模型判断无经验 / 模型不可用时都不产生提议。
"""

import uuid

from typing import cast

import httpx
import pytest
from sqlalchemy import select

from app.agents.models import AgentRun, AgentSuggestion
from app.agents.service import submit_suggestion_feedback
from app.domains.memory import summary as summary_module
from app.domains.memory.core_memory import list_entries
from app.domains.memory.proposals import MEMORY_PROPOSAL_TYPE
from app.domains.memory.summary import EXPERIENCE_SUMMARY_AGENT_TYPE
from app.domains.project.models import Project, ProjectMember
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.queue.queue import DELAYED_QUEUE_KEY, QUEUE_KEY
from app.workers.memory_summary import execute_memory_summary
from tests.test_memory_summary import FakeModelProvider, UnavailableModelProvider, _completed_item


@pytest.fixture
async def redis_client():
    client = create_redis_client()
    await client.delete(QUEUE_KEY, DELAYED_QUEUE_KEY)
    yield client
    await client.delete(QUEUE_KEY, DELAYED_QUEUE_KEY)
    await client.aclose()


async def _run_summary(
    client: httpx.AsyncClient, project: Project, redis_client, monkeypatch, text: str
) -> tuple[dict, str]:
    """完成工作项并执行总结任务。"""
    monkeypatch.setattr(
        summary_module, "get_model_provider", lambda: FakeModelProvider(text=text)
    )
    ctx, item_id = await _completed_item(client, project)
    await redis_client.delete(QUEUE_KEY, DELAYED_QUEUE_KEY)
    await execute_memory_summary({"work_item_id": item_id})
    return ctx, item_id


async def test_summary_produces_pending_proposal(
    client: httpx.AsyncClient, project: Project, redis_client, monkeypatch
) -> None:
    _, item_id = await _run_summary(
        client, project, redis_client, monkeypatch, "导入类需求后端占比最大"
    )

    async with async_session_factory() as session:
        proposals = (
            await session.execute(
                select(AgentSuggestion).where(
                    AgentSuggestion.suggestion_type == MEMORY_PROPOSAL_TYPE
                )
            )
        ).scalars().all()
        assert len(proposals) == 1
        proposal = proposals[0]
        assert proposal.review_status == "pending"
        assert proposal.content["action"] == "create"
        assert proposal.content["content"] == "导入类需求后端占比最大"
        assert "自动沉淀的经验" in proposal.content["reason"]

        run = await session.get(AgentRun, proposal.run_id)
        assert run is not None
        assert run.agent_type == EXPERIENCE_SUMMARY_AGENT_TYPE
        assert run.trigger_source == "event"
        assert run.work_item_id == uuid.UUID(item_id)
        assert run.project_id == project.id

        assert await list_entries(session, project_id=project.id) == []


async def test_confirm_summary_proposal_enters_core_memory(
    client: httpx.AsyncClient, project: Project, redis_client, monkeypatch
) -> None:
    """工作项经验经总结和负责人确认后应进入核心记忆。"""
    ctx, _ = await _run_summary(client, project, redis_client, monkeypatch, "经验一条")

    leader = cast(ProjectMember, ctx["leader"])  # _setup 创建的负责人（每项目一名负责人）
    async with async_session_factory() as session:
        proposal = (
            await session.execute(
                select(AgentSuggestion).where(
                    AgentSuggestion.suggestion_type == MEMORY_PROPOSAL_TYPE
                )
            )
        ).scalar_one()
        await submit_suggestion_feedback(session, proposal, action="accepted", member=leader)

    async with async_session_factory() as session:
        entries = await list_entries(session, project_id=project.id)
        assert len(entries) == 1
        assert entries[0].content == "经验一条"
        assert entries[0].status == "active"
        assert entries[0].proposed_by_member_id is None  # Agent 提议
        assert entries[0].confirmed_by_member_id == leader.id


async def test_reject_summary_proposal_keeps_memory_unchanged(
    client: httpx.AsyncClient, project: Project, redis_client, monkeypatch
) -> None:
    ctx, _ = await _run_summary(client, project, redis_client, monkeypatch, "经验一条")

    leader = cast(ProjectMember, ctx["leader"])
    async with async_session_factory() as session:
        proposal = (
            await session.execute(
                select(AgentSuggestion).where(
                    AgentSuggestion.suggestion_type == MEMORY_PROPOSAL_TYPE
                )
            )
        ).scalar_one()
        await submit_suggestion_feedback(session, proposal, action="ignored", member=leader)
        assert await list_entries(session, project_id=project.id) == []


async def test_no_experience_no_proposal(
    client: httpx.AsyncClient, project: Project, redis_client, monkeypatch
) -> None:
    await _run_summary(client, project, redis_client, monkeypatch, "无")
    async with async_session_factory() as session:
        count = (
            await session.execute(
                select(AgentSuggestion).where(
                    AgentSuggestion.suggestion_type == MEMORY_PROPOSAL_TYPE
                )
            )
        ).scalars().all()
        assert count == []


async def test_model_unavailable_no_proposal(
    client: httpx.AsyncClient, project: Project, redis_client, monkeypatch
) -> None:
    monkeypatch.setattr(
        summary_module, "get_model_provider", lambda: UnavailableModelProvider()
    )
    _, item_id = await _completed_item(client, project)
    await execute_memory_summary({"work_item_id": item_id})
    async with async_session_factory() as session:
        count = (
            await session.execute(
                select(AgentSuggestion).where(
                    AgentSuggestion.suggestion_type == MEMORY_PROPOSAL_TYPE
                )
            )
        ).scalars().all()
        assert count == []
