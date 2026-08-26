"""核心记忆提议自动过期测试（M4.5 验收，设计文档 16.6）。

- 挂起超 7 天的 memory_proposal 标记 expired（终态），留审计；
- 未超期/非提议类型/已反馈的建议不受影响；
- 过期提议不占待确认列表、不可再确认（409）；同内容可重新提议。
"""

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select, update

from app.agents.models import AgentRun, AgentSuggestion
from app.domains.audit.models import AuditEvent
from app.domains.memory.proposals import (
    MEMORY_PROPOSAL_EXPIRE_DAYS,
    MEMORY_PROPOSAL_TYPE,
    create_memory_proposal,
    expire_stale_proposals,
)
from app.domains.project.models import Project, ProjectMember
from app.infrastructure.database.engine import async_session_factory
from app.workers.proposal_expire import expire_memory_proposals
from tests.conftest import auth_headers

LEADER_PW = "Leader123!"


async def _make_run(project_id) -> AgentRun:
    async with async_session_factory() as session:
        run = AgentRun(agent_type="requirement_analyst", project_id=project_id, prompt="")
        session.add(run)
        await session.commit()
        return run


async def _backdate(suggestion_id, *, days: float) -> None:
    """把提议的创建时间回拨到 N 天前（造超期场景）。"""
    async with async_session_factory() as session:
        await session.execute(
            update(AgentSuggestion)
            .where(AgentSuggestion.id == suggestion_id)
            .values(created_at=datetime.now(UTC) - timedelta(days=days))
        )
        await session.commit()


async def test_stale_proposal_expired(project_a: Project, leader: ProjectMember) -> None:
    run = await _make_run(project_a.id)
    async with async_session_factory() as session:
        fresh = await create_memory_proposal(session, run=run, action="create", content="新提议")
        stale = await create_memory_proposal(session, run=run, action="create", content="旧提议")
        # 非提议类型的挂起建议不受影响
        other = AgentSuggestion(
            run_id=run.id, suggestion_type="planning", content={"x": 1}
        )
        session.add(other)
        await session.commit()
        stale_id, fresh_id, other_id = stale.id, fresh.id, other.id

    await _backdate(stale_id, days=MEMORY_PROPOSAL_EXPIRE_DAYS + 1)
    await _backdate(other_id, days=MEMORY_PROPOSAL_EXPIRE_DAYS + 1)

    async with async_session_factory() as session:
        expired = await expire_stale_proposals(session)
        assert expired == 1

    async with async_session_factory() as session:
        stale_row = await session.get(AgentSuggestion, stale_id)
        assert stale_row is not None
        assert stale_row.review_status == "expired"
        assert stale_row.reviewed_at is not None
        fresh_row = await session.get(AgentSuggestion, fresh_id)
        assert fresh_row is not None and fresh_row.review_status == "pending"
        other_row = await session.get(AgentSuggestion, other_id)
        assert other_row is not None and other_row.review_status == "pending"
        # 审计留痕（16.10，系统动作 actor 为空）
        event = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "core_memory.proposal_expired",
                    AuditEvent.target_id == stale_id,
                )
            )
        ).scalar_one()
        assert event.actor_id is None
        assert event.project_id == project_a.id


async def test_expire_boundary_not_expired(project_a: Project, leader: ProjectMember) -> None:
    """恰好 7 天边界内（< 7 天）不过期。"""
    run = await _make_run(project_a.id)
    async with async_session_factory() as session:
        proposal = await create_memory_proposal(session, run=run, action="create", content="边界")
        pid = proposal.id
    await _backdate(pid, days=MEMORY_PROPOSAL_EXPIRE_DAYS - 0.5)
    async with async_session_factory() as session:
        assert await expire_stale_proposals(session) == 0


async def test_expired_proposal_not_confirmable_and_reproposable(
    client: httpx.AsyncClient, project_a: Project, leader: ProjectMember
) -> None:
    run = await _make_run(project_a.id)
    async with async_session_factory() as session:
        proposal = await create_memory_proposal(
            session, run=run, action="create", content="某条经验"
        )
        pid = proposal.id
    await _backdate(pid, days=MEMORY_PROPOSAL_EXPIRE_DAYS + 1)
    await expire_memory_proposals()  # 走 worker 处理器路径

    headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project_a.id))
    # 过期提议从待确认列表消失
    resp = await client.get(
        "/api/v1/agent-suggestions", headers=headers, params={"review_status": "pending"}
    )
    assert str(pid) not in [s["id"] for s in resp.json()]
    # 过期提议可查到（追溯），状态 expired
    resp = await client.get(
        "/api/v1/agent-suggestions", headers=headers, params={"review_status": "expired"}
    )
    assert str(pid) in [s["id"] for s in resp.json()]
    # 过期后不可再确认 → 409
    resp = await client.post(
        f"/api/v1/agent-suggestions/{pid}/feedback",
        headers=headers,
        json={"action": "accepted"},
    )
    assert resp.status_code == 409

    # 同内容可重新提议（Agent 认为仍重要，16.6）
    async with async_session_factory() as session:
        new_proposal = await create_memory_proposal(
            session, run=run, action="create", content="某条经验"
        )
        assert new_proposal.id != pid
        assert new_proposal.review_status == "pending"


async def test_worker_handler_no_stale(project_a: Project, leader: ProjectMember) -> None:
    """无超期提议时 worker 处理器正常空转。"""
    run = await _make_run(project_a.id)
    async with async_session_factory() as session:
        await create_memory_proposal(session, run=run, action="create", content="x")
    await expire_memory_proposals()  # 不抛异常
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(AgentSuggestion).where(
                    AgentSuggestion.suggestion_type == MEMORY_PROPOSAL_TYPE
                )
            )
        ).scalars().all()
        assert [r.review_status for r in rows] == ["pending"]
