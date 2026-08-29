"""核心记忆整合提议的预算、校验、回滚与审计测试。

- consolidate 负载：确认后旧条目作废、新条目生效，预算按"旧腾出+新占用"净额校验；
- 目标条目缺失/跨项目 404、含已作废条目 409，均整体回滚且建议保持 pending；
- 负载校验：少于两条目标 / 缺精简内容 → 400；
- budget_nearly_full 容量快满判断返回布尔值、占用量和预算。
"""

import pytest
from sqlalchemy import select

from app.agents.models import AgentRun, AgentSuggestion
from app.agents.service import submit_suggestion_feedback
from app.core.errors import ApiException, ErrorCodes
from app.domains.audit.models import AuditEvent
from app.domains.memory.core_memory import (
    budget_nearly_full,
    budget_usage,
    create_entry,
    deprecate_entry,
    list_entries,
)
from app.domains.memory.models import CORE_MEMORY_BUDGET_CHARS, CORE_MEMORY_NEAR_FULL_RATIO
from app.domains.memory.proposals import create_memory_proposal
from app.domains.project.models import Project, ProjectMember
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member


async def _make_run(project_id) -> AgentRun:
    async with async_session_factory() as session:
        run = AgentRun(agent_type="requirement_analyst", project_id=project_id, prompt="")
        session.add(run)
        await session.commit()
        return run


async def test_consolidate_confirm_flow(project_a: Project, leader: ProjectMember) -> None:
    run = await _make_run(project_a.id)
    async with async_session_factory() as session:
        e1 = await create_entry(session, leader, content="约定一：禁用递归查询")
        e2 = await create_entry(session, leader, content="约定二：禁用嵌套事务")
        proposal = await create_memory_proposal(
            session,
            run=run,
            action="consolidate",
            entry_ids=[e1.id, e2.id],
            content="约定：禁止递归查询与嵌套事务",
            reason="两条约定语义相近，合并精简",
        )
        await submit_suggestion_feedback(session, proposal, action="accepted", member=leader)

    async with async_session_factory() as session:
        entries = await list_entries(session, project_id=project_a.id)
        active = [e for e in entries if e.status == "active"]
        deprecated = [e for e in entries if e.status == "deprecated"]
        assert len(active) == 1 and active[0].content == "约定：禁止递归查询与嵌套事务"
        assert {e.id for e in deprecated} == {e1.id, e2.id}
        assert active[0].proposed_by_member_id is None  # Agent 提议
        assert active[0].confirmed_by_member_id == leader.id

        events = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.action.in_(
                        ["core_memory.deprecated", "core_memory.created"]
                    ),
                    AuditEvent.project_id == project_a.id,
                )
            )
        ).scalars().all()
        created = [
            e
            for e in events
            if e.action == "core_memory.created"
            and e.after is not None
            and "consolidates" in e.after
        ]
        assert len(created) == 1
        assert len([e for e in events if e.action == "core_memory.deprecated"]) == 2
        assert created[0].after is not None
        assert set(created[0].after["consolidates"]) == {str(e1.id), str(e2.id)}

        used, _ = await budget_usage(session, project_id=project_a.id)
        assert used == len("约定：禁止递归查询与嵌套事务")


async def test_consolidate_net_budget_check(project_a: Project, leader: ProjectMember) -> None:
    """预算接近用满时，整合释放的空间应计入可用净额。"""
    run = await _make_run(project_a.id)
    async with async_session_factory() as session:
        filler = await create_entry(
            session, leader, content="f" * (CORE_MEMORY_BUDGET_CHARS - 40)
        )
        e1 = await create_entry(session, leader, content="短约定一")  # 5
        e2 = await create_entry(session, leader, content="短约定二")  # 5
        # 当前占用 = budget - 30；整合后新内容 20 字 < 腾出 10 + 余量 30，应通过
        proposal = await create_memory_proposal(
            session,
            run=run,
            action="consolidate",
            entry_ids=[e1.id, e2.id],
            content="x" * 20,
        )
        await submit_suggestion_feedback(session, proposal, action="accepted", member=leader)

    async with async_session_factory() as session:
        entries = await list_entries(session, project_id=project_a.id)
        active = [e for e in entries if e.status == "active"]
        assert len(active) == 2
        assert {e.content for e in active} == {filler.content, "x" * 20}


async def test_consolidate_over_budget_rejected(project_a: Project, leader: ProjectMember) -> None:
    """新内容超过净额预算时应拒绝、整体回滚并保留待处理状态。"""
    run = await _make_run(project_a.id)
    async with async_session_factory() as session:
        await create_entry(session, leader, content="f" * (CORE_MEMORY_BUDGET_CHARS - 10))
        e1 = await create_entry(session, leader, content="短一")  # 2 字
        e2 = await create_entry(session, leader, content="短二")  # 2 字
        # 净额 = 20 - 4 = 16 > 余量 10 → 超预算
        proposal = await create_memory_proposal(
            session,
            run=run,
            action="consolidate",
            entry_ids=[e1.id, e2.id],
            content="x" * 20,
        )
        with pytest.raises(ApiException) as exc_info:
            await submit_suggestion_feedback(session, proposal, action="accepted", member=leader)
        assert exc_info.value.code == ErrorCodes.CORE_MEMORY_BUDGET_EXCEEDED
        entries = await list_entries(session, project_id=project_a.id)
        assert all(e.status == "active" for e in entries)

    async with async_session_factory() as session:
        row = await session.get(AgentSuggestion, proposal.id)
        assert row is not None and row.review_status == "pending"


async def test_consolidate_stale_or_cross_project_rollback(
    project_a: Project, project_b: Project, leader: ProjectMember
) -> None:
    _, leader_b = await add_member(project_b, "leaderb", "LeaderB123!", role="leader")
    run = await _make_run(project_a.id)

    # 跨项目目标不得暴露其存在性，也不得改变本项目条目。
    async with async_session_factory() as session:
        e_a = await create_entry(session, leader, content="A 的约定")
        e_b = await create_entry(session, leader_b, content="B 的约定")
        proposal = await create_memory_proposal(
            session, run=run, action="consolidate", entry_ids=[e_a.id, e_b.id], content="合并"
        )
        with pytest.raises(ApiException) as exc_info:
            await submit_suggestion_feedback(session, proposal, action="accepted", member=leader)
        assert exc_info.value.status_code == 404
        a_entries = await list_entries(session, project_id=project_a.id)
        assert a_entries[0].status == "active"

    async with async_session_factory() as session:
        e1 = await create_entry(session, leader, content="一")
        e2 = await create_entry(session, leader, content="二")
        await deprecate_entry(session, leader, entry_id=e2.id)
        proposal = await create_memory_proposal(
            session, run=run, action="consolidate", entry_ids=[e1.id, e2.id], content="合并"
        )
        with pytest.raises(ApiException) as exc_info:
            await submit_suggestion_feedback(session, proposal, action="accepted", member=leader)
        assert exc_info.value.status_code == 409
        row = await session.get(AgentSuggestion, proposal.id)
        assert row is not None and row.review_status == "pending"


async def test_consolidate_payload_validation(
    project_a: Project, leader: ProjectMember
) -> None:
    run = await _make_run(project_a.id)
    async with async_session_factory() as session:
        e1 = await create_entry(session, leader, content="一")
        e2 = await create_entry(session, leader, content="二")
        with pytest.raises(ApiException) as exc_info:
            await create_memory_proposal(
                session, run=run, action="consolidate", entry_ids=[e1.id], content="x"
            )
        assert exc_info.value.status_code == 400
        with pytest.raises(ApiException):
            await create_memory_proposal(
                session, run=run, action="consolidate", entry_ids=[e1.id, e1.id], content="x"
            )
        with pytest.raises(ApiException):
            await create_memory_proposal(
                session, run=run, action="consolidate", entry_ids=[e1.id, e2.id]
            )


async def test_budget_nearly_full(project_a: Project, leader: ProjectMember) -> None:
    async with async_session_factory() as session:
        nearly, used, budget = await budget_nearly_full(session, project_id=project_a.id)
        assert nearly is False and used == 0 and budget == CORE_MEMORY_BUDGET_CHARS

        await create_entry(
            session,
            leader,
            content="x" * int(CORE_MEMORY_BUDGET_CHARS * CORE_MEMORY_NEAR_FULL_RATIO),
        )
        nearly, used, budget = await budget_nearly_full(session, project_id=project_a.id)
        assert nearly is True
        assert used == int(CORE_MEMORY_BUDGET_CHARS * CORE_MEMORY_NEAR_FULL_RATIO)
