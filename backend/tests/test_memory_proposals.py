"""核心记忆提议测试（M4.4 验收，设计文档第 8 节）。

- 提议 → 确认 → 生效全链路：create/update/deprecate 三类负载；
- 未确认前核心记忆不变（红线：Agent 不直接改数据）；拒绝（ignored）只记录；
- 确认时校验失败（容量超限/条目已作废/跨项目）整体回滚，建议保持 pending；
- 提议与确认均入审计域（16.10）。
"""

import asyncio

import httpx
import pytest
from sqlalchemy import select

from app.agents.models import AgentRun, AgentSuggestion
from app.agents.service import submit_suggestion_feedback
from app.core.errors import ApiException, ErrorCodes
from app.domains.audit.models import AuditEvent
from app.domains.memory.core_memory import create_entry, deprecate_entry, list_entries
from app.domains.memory.models import CORE_MEMORY_BUDGET_CHARS
from app.domains.memory.proposals import (
    MEMORY_PROPOSAL_TYPE,
    create_memory_proposal,
)
from app.domains.project.models import Project, ProjectMember
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member, auth_headers

LEADER_PW = "Leader123!"


async def _make_run(project_id) -> AgentRun:
    """直接建库创建一条 Agent 运行记录（提议须挂 run，FK 约束）。"""
    async with async_session_factory() as session:
        run = AgentRun(agent_type="requirement_analyst", project_id=project_id, prompt="")
        session.add(run)
        await session.commit()
        return run


async def _get_suggestion(suggestion_id) -> AgentSuggestion | None:
    async with async_session_factory() as session:
        return await session.get(AgentSuggestion, suggestion_id)


async def test_create_proposal_confirm_flow(project_a: Project, leader: ProjectMember) -> None:
    run = await _make_run(project_a.id)
    async with async_session_factory() as session:
        proposal = await create_memory_proposal(
            session,
            run=run,
            action="create",
            content="改 X 表结构一定要同步改 Y",
            reason="踩坑教训",
        )
        assert proposal.suggestion_type == MEMORY_PROPOSAL_TYPE
        assert proposal.review_status == "pending"
        # 未确认前核心记忆不变
        assert await list_entries(session, project_id=project_a.id) == []

        # 负责人确认 → 生效
        await submit_suggestion_feedback(session, proposal, action="accepted", member=leader)
        assert proposal.review_status == "accepted"

    async with async_session_factory() as session:
        entries = await list_entries(session, project_id=project_a.id)
        assert len(entries) == 1
        entry = entries[0]
        assert entry.content == "改 X 表结构一定要同步改 Y"
        assert entry.status == "active"
        assert entry.proposed_by_member_id is None  # Agent 提议
        assert entry.confirmed_by_member_id == leader.id

        actions = (
            await session.execute(
                select(AuditEvent.action).where(
                    AuditEvent.target_id.in_([proposal.id, entry.id])
                )
            )
        ).scalars().all()
        assert "core_memory.proposed" in actions
        assert "core_memory.created" in actions
        assert "agent.suggestion_feedback" in actions


async def test_reject_keeps_memory_unchanged(project_a: Project, leader: ProjectMember) -> None:
    run = await _make_run(project_a.id)
    async with async_session_factory() as session:
        proposal = await create_memory_proposal(
            session, run=run, action="create", content="某条经验"
        )
        await submit_suggestion_feedback(session, proposal, action="ignored", member=leader)
        assert proposal.review_status == "ignored"
        assert await list_entries(session, project_id=project_a.id) == []


async def test_update_proposal_replaces_content(
    project_a: Project, leader: ProjectMember
) -> None:
    run = await _make_run(project_a.id)
    async with async_session_factory() as session:
        entry = await create_entry(session, leader, content="旧约定")
        proposal = await create_memory_proposal(
            session, run=run, action="update", entry_id=entry.id, content="新约定"
        )
        await submit_suggestion_feedback(session, proposal, action="accepted", member=leader)

    async with async_session_factory() as session:
        entries = await list_entries(session, project_id=project_a.id)
        assert len(entries) == 1
        assert entries[0].content == "新约定"
        assert entries[0].confirmed_by_member_id == leader.id
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "core_memory.updated",
                    AuditEvent.target_id == entry.id,
                )
            )
        ).scalar_one()
        assert event.before == {"content": "旧约定"}


async def test_deprecate_proposal(project_a: Project, leader: ProjectMember) -> None:
    run = await _make_run(project_a.id)
    async with async_session_factory() as session:
        entry = await create_entry(session, leader, content="过时约定")
        proposal = await create_memory_proposal(
            session, run=run, action="deprecate", entry_id=entry.id
        )
        await submit_suggestion_feedback(session, proposal, action="accepted", member=leader)
        entries = await list_entries(session, project_id=project_a.id)
        assert entries[0].status == "deprecated"


async def test_confirm_over_budget_rolls_back(project_a: Project, leader: ProjectMember) -> None:
    run = await _make_run(project_a.id)
    async with async_session_factory() as session:
        await create_entry(session, leader, content="x" * (CORE_MEMORY_BUDGET_CHARS - 10))
        proposal = await create_memory_proposal(
            session, run=run, action="create", content="y" * 20
        )
        with pytest.raises(ApiException) as exc_info:
            await submit_suggestion_feedback(session, proposal, action="accepted", member=leader)
        assert exc_info.value.code == ErrorCodes.CORE_MEMORY_BUDGET_EXCEEDED

    # 回滚：建议保持 pending，核心记忆无新条目
    suggestion = await _get_suggestion(proposal.id)
    assert suggestion is not None
    assert suggestion.review_status == "pending"
    async with async_session_factory() as session:
        assert len(await list_entries(session, project_id=project_a.id)) == 1


async def test_confirm_stale_entry_conflict(project_a: Project, leader: ProjectMember) -> None:
    run = await _make_run(project_a.id)
    async with async_session_factory() as session:
        entry = await create_entry(session, leader, content="将被抢先作废")
        proposal = await create_memory_proposal(
            session, run=run, action="deprecate", entry_id=entry.id
        )
        # 提议后条目被负责人先手工作废
        await deprecate_entry(session, leader, entry_id=entry.id)
        with pytest.raises(ApiException) as exc_info:
            await submit_suggestion_feedback(session, proposal, action="accepted", member=leader)
        assert exc_info.value.status_code == 409

    suggestion = await _get_suggestion(proposal.id)
    assert suggestion is not None
    assert suggestion.review_status == "pending"


async def test_confirm_cross_project_entry_not_found(
    project_a: Project, project_b: Project, leader: ProjectMember
) -> None:
    _, leader_b = await add_member(project_b, "leaderb", "LeaderB123!", role="leader")
    run_a = await _make_run(project_a.id)
    async with async_session_factory() as session:
        entry_b = await create_entry(session, leader_b, content="B 项目约定")
        proposal = await create_memory_proposal(
            session, run=run_a, action="deprecate", entry_id=entry_b.id
        )
        with pytest.raises(ApiException) as exc_info:
            await submit_suggestion_feedback(session, proposal, action="accepted", member=leader)
        assert exc_info.value.status_code == 404


async def test_concurrent_feedback_only_applies_memory_proposal_once(
    client: httpx.AsyncClient, project_a: Project, leader: ProjectMember
) -> None:
    """并发 accepted 只允许一个请求获得建议处理权，不能重复创建核心记忆。"""
    run = await _make_run(project_a.id)
    async with async_session_factory() as session:
        proposal = await create_memory_proposal(
            session, run=run, action="create", content="并发确认只能创建一次"
        )
    headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project_a.id))

    responses = await asyncio.gather(
        *[
            client.post(
                f"/api/v1/agent-suggestions/{proposal.id}/feedback",
                headers=headers,
                json={"action": "accepted"},
            )
            for _ in range(2)
        ]
    )

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert any(
        response.json().get("code") == ErrorCodes.AGENT_SUGGESTION_ALREADY_REVIEWED
        for response in responses
        if response.status_code == 409
    )
    async with async_session_factory() as session:
        entries = await list_entries(session, project_id=project_a.id)
        suggestion = await session.get(AgentSuggestion, proposal.id)
    assert len(entries) == 1
    assert entries[0].content == "并发确认只能创建一次"
    assert suggestion is not None
    assert suggestion.review_status == "accepted"


async def test_concurrent_conflicting_feedback_cannot_apply_ignored_proposal(
    client: httpx.AsyncClient, project_a: Project, leader: ProjectMember
) -> None:
    """accepted 与 ignored 并发时，最终状态和核心记忆副作用必须一致。"""
    run = await _make_run(project_a.id)
    async with async_session_factory() as session:
        proposal = await create_memory_proposal(
            session, run=run, action="create", content="冲突反馈不应留下幽灵记忆"
        )
    headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project_a.id))

    accepted, ignored = await asyncio.gather(
        client.post(
            f"/api/v1/agent-suggestions/{proposal.id}/feedback",
            headers=headers,
            json={"action": "accepted"},
        ),
        client.post(
            f"/api/v1/agent-suggestions/{proposal.id}/feedback",
            headers=headers,
            json={"action": "ignored"},
        ),
    )

    assert sorted(response.status_code for response in (accepted, ignored)) == [200, 409]
    async with async_session_factory() as session:
        entries = await list_entries(session, project_id=project_a.id)
        suggestion = await session.get(AgentSuggestion, proposal.id)
    assert suggestion is not None
    assert suggestion.review_status in {"accepted", "ignored"}
    assert len(entries) == (1 if suggestion.review_status == "accepted" else 0)


async def test_feedback_endpoint_applies_proposal(
    client: httpx.AsyncClient, project_a: Project, leader: ProjectMember
) -> None:
    """API 全链路：建议中心确认 memory_proposal → 核心记忆生效。"""
    run = await _make_run(project_a.id)
    async with async_session_factory() as session:
        proposal = await create_memory_proposal(
            session, run=run, action="create", content="API 链路验证"
        )
    headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project_a.id))
    resp = await client.post(
        f"/api/v1/agent-suggestions/{proposal.id}/feedback",
        headers=headers,
        json={"action": "accepted"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["review_status"] == "accepted"

    resp = await client.get("/api/v1/memory/core-entries", headers=headers)
    assert [e["content"] for e in resp.json()["entries"]] == ["API 链路验证"]
