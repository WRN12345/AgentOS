"""核心记忆条目服务测试。

- 负责人手写条目立即生效，来源信息（提议者/确认者/生效时间）完整可查；
- 非负责人写被拒；项目成员可读（含已作废条目，供追溯）；
- 容量预算：单项目生效条目合计超 4000 字符拒绝，并提示走整合精简；
- 作废释放容量；跨项目访问按 404；重复作废 409；
- 手写/作废写入审计事件（16.10）。
"""

import uuid

import pytest
from sqlalchemy import select

from app.core.errors import ApiException, ErrorCodes
from app.domains.audit.models import AuditEvent
from app.domains.memory.core_memory import (
    budget_usage,
    create_entry,
    deprecate_entry,
    list_entries,
)
from app.domains.memory.models import CORE_MEMORY_BUDGET_CHARS
from app.domains.project.models import Project, ProjectMember
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member


async def test_leader_create_entry_effective_immediately(
    project_a: Project, leader: ProjectMember
) -> None:
    async with async_session_factory() as session:
        entry = await create_entry(session, leader, content=" 本项目禁用递归查询 ")
        assert entry.status == "active"
        assert entry.scope == "project"
        assert entry.project_id == project_a.id
        assert entry.proposed_by_member_id == leader.id
        assert entry.confirmed_by_member_id == leader.id
        assert entry.effective_at is not None
        assert entry.content == "本项目禁用递归查询"  # 首尾空白被清理

    async with async_session_factory() as session:
        entries = await list_entries(session, project_id=project_a.id)
        assert [e.id for e in entries] == [entry.id]
        used, budget = await budget_usage(session, project_id=project_a.id)
        assert used == len("本项目禁用递归查询")
        assert budget == CORE_MEMORY_BUDGET_CHARS


async def test_non_leader_create_forbidden(project_a: Project) -> None:
    _, member = await add_member(project_a, "bob", "Bob12345!")
    async with async_session_factory() as session:
        with pytest.raises(ApiException) as exc_info:
            await create_entry(session, member, content="x")
        assert exc_info.value.status_code == 403


async def test_budget_exceeded_rejected(project_a: Project, leader: ProjectMember) -> None:
    async with async_session_factory() as session:
        await create_entry(session, leader, content="x" * (CORE_MEMORY_BUDGET_CHARS - 10))
        with pytest.raises(ApiException) as exc_info:
            await create_entry(session, leader, content="y" * 20)
        err = exc_info.value
        assert err.status_code == 400
        assert err.code == ErrorCodes.CORE_MEMORY_BUDGET_EXCEEDED
        assert "整合精简" in err.message
        assert err.details["used"] == CORE_MEMORY_BUDGET_CHARS - 10
        assert err.details["budget"] == CORE_MEMORY_BUDGET_CHARS
        assert err.details["required"] == 20
        # 被拒后占用不变
        used, _ = await budget_usage(session, project_id=project_a.id)
        assert used == CORE_MEMORY_BUDGET_CHARS - 10


async def test_deprecate_frees_budget_and_keeps_trace(
    project_a: Project, leader: ProjectMember
) -> None:
    async with async_session_factory() as session:
        old = await create_entry(session, leader, content="z" * CORE_MEMORY_BUDGET_CHARS)
        await deprecate_entry(session, leader, entry_id=old.id)
        assert old.status == "deprecated"
        # 作废后容量释放，可再写
        new = await create_entry(session, leader, content="新约定")
        # 列表仍可查已作废条目（追溯），生效条目在前
        entries = await list_entries(session, project_id=project_a.id)
        assert [e.id for e in entries] == [new.id, old.id]
        used, _ = await budget_usage(session, project_id=project_a.id)
        assert used == len("新约定")


async def test_deprecate_permission_and_isolation(
    project_a: Project, project_b: Project, leader: ProjectMember
) -> None:
    _, member = await add_member(project_a, "carol", "Carol123!")
    _, leader_b = await add_member(project_b, "leaderb", "LeaderB123!", role="leader")

    async with async_session_factory() as session:
        entry = await create_entry(session, leader, content="约定")
        with pytest.raises(ApiException) as exc_info:
            await deprecate_entry(session, member, entry_id=entry.id)
        assert exc_info.value.status_code == 403
        with pytest.raises(ApiException) as exc_info:
            await deprecate_entry(session, leader_b, entry_id=entry.id)
        assert exc_info.value.status_code == 404
        await deprecate_entry(session, leader, entry_id=entry.id)
        with pytest.raises(ApiException) as exc_info:
            await deprecate_entry(session, leader, entry_id=entry.id)
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == ErrorCodes.CORE_MEMORY_INVALID_TRANSITION


async def test_audit_events_written(project_a: Project, leader: ProjectMember) -> None:
    async with async_session_factory() as session:
        entry = await create_entry(session, leader, content="支付模块走独立服务")
        await deprecate_entry(session, leader, entry_id=entry.id)

    async with async_session_factory() as session:
        events = (
            await session.execute(
                select(AuditEvent)
                .where(AuditEvent.target_id == entry.id)
                .order_by(AuditEvent.created_at)
            )
        ).scalars().all()
        assert [e.action for e in events] == ["core_memory.created", "core_memory.deprecated"]
        assert all(e.actor_id == leader.user_id for e in events)
        assert all(e.project_id == project_a.id for e in events)
        assert events[0].after is not None and events[0].after["source"] == "manual"


async def test_empty_content_rejected(project_a: Project, leader: ProjectMember) -> None:
    async with async_session_factory() as session:
        with pytest.raises(ApiException) as exc_info:
            await create_entry(session, leader, content="   ")
        assert exc_info.value.status_code == 400
        assert exc_info.value.code == ErrorCodes.VALIDATION_ERROR
