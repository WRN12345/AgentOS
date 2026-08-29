"""成员完成数、按时率与当前负载的项目内统计测试。

- 按项目+成员聚合：完成总数（主执行人口径）、当前活跃负载、近 30 天完成数；
- 跨项目数据不混入；停用成员统计保留。
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app.domains.memory.member_stats import (
    MIN_SAMPLE_SIZE,
    RECENT_DAYS,
    member_completion_stats,
)
from app.domains.project.models import Project, ProjectMember
from app.domains.work_items.models import WorkItem
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member, add_member_for_existing_user


async def _add_item(
    project: Project, assignee: ProjectMember, status: str, *, title: str = "任务",
    due_at: datetime | None = None,
) -> uuid.UUID:
    async with async_session_factory() as session:
        item = WorkItem(
            project_id=project.id, title=title, assignee_id=assignee.id, status=status,
            due_at=due_at,
        )
        session.add(item)
        await session.commit()
        return item.id


async def _backdate_updated(item_id: uuid.UUID, *, days: float) -> None:
    """把 updated_at 回拨到 N 天前（模拟较早完成的项）。"""
    async with async_session_factory() as session:
        await session.execute(
            update(WorkItem)
            .where(WorkItem.id == item_id)
            .values(updated_at=datetime.now(UTC) - timedelta(days=days))
        )
        await session.commit()


async def test_stats_aggregation(project_a: Project) -> None:
    _, alice = await add_member(project_a, "alice", "Alice123!", display_name="爱丽丝")
    _, bob = await add_member(project_a, "bob", "Bob12345!", display_name="鲍勃")

    old_done = await _add_item(project_a, alice, "COMPLETED", title="早期完成")
    await _add_item(project_a, alice, "COMPLETED", title="刚完成")
    await _add_item(project_a, alice, "IN_PROGRESS", title="进行中")
    await _backdate_updated(old_done, days=RECENT_DAYS + 10)  # 超出近期窗口
    await _add_item(project_a, bob, "IN_PROGRESS", title="鲍勃的活一")
    await _add_item(project_a, bob, "BLOCKED", title="鲍勃的活二")
    await _add_item(project_a, bob, "CANCELLED", title="取消不计入")

    async with async_session_factory() as session:
        stats = {s.display_name: s for s in await member_completion_stats(session, project_id=project_a.id)}

    assert stats["爱丽丝"].completed_total == 2
    assert stats["爱丽丝"].active_now == 1
    assert stats["爱丽丝"].completed_recent == 1  # 超窗的那条不算近期
    assert stats["鲍勃"].completed_total == 0
    assert stats["鲍勃"].active_now == 2


async def test_stats_project_isolation(project_a: Project, project_b: Project) -> None:
    """同一用户在不同项目的统计应严格隔离。"""
    user_a, member_a = await add_member(project_a, "carol", "Carol123!", display_name="卡罗尔")
    member_b = await add_member_for_existing_user(
        async_session_factory, project_b, user_a, display_name="卡罗尔"
    )
    await _add_item(project_a, member_a, "COMPLETED")
    for _ in range(3):
        await _add_item(project_b, member_b, "COMPLETED")

    async with async_session_factory() as session:
        stats_a = await member_completion_stats(session, project_id=project_a.id)
        stats_b = await member_completion_stats(session, project_id=project_b.id)

    sa = next(s for s in stats_a if s.member_id == member_a.id)
    sb = next(s for s in stats_b if s.member_id == member_b.id)
    assert sa.completed_total == 1
    assert sb.completed_total == 3


async def test_inactive_member_stats_kept(project_a: Project) -> None:
    """停用成员的统计应保留并标记为停用。"""
    _, dave = await add_member(project_a, "dave", "Dave12345!", display_name="戴夫")
    await _add_item(project_a, dave, "COMPLETED")
    async with async_session_factory() as session:
        member = await session.get(ProjectMember, dave.id)
        assert member is not None
        member.is_active = False
        await session.commit()

        stats = await member_completion_stats(session, project_id=project_a.id)
        sd = next(s for s in stats if s.member_id == dave.id)
        assert sd.is_active is False
        assert sd.completed_total == 1


async def test_on_time_rate_wide_scope(project_a: Project) -> None:
    """逾期完成、改期后按时（最终截止时间）、无截止时间三种边界。"""
    _, alice = await add_member(project_a, "alice", "Alice123!", display_name="爱丽丝")
    now = datetime.now(UTC)
    await _add_item(project_a, alice, "COMPLETED", title="逾期", due_at=now - timedelta(days=1))
    # 改期后以最终截止时间判断是否按时。
    await _add_item(project_a, alice, "COMPLETED", title="改期后按时", due_at=now + timedelta(days=1))
    await _add_item(project_a, alice, "COMPLETED", title="无截止")
    await _add_item(project_a, alice, "CANCELLED", title="取消", due_at=now - timedelta(days=1))

    async with async_session_factory() as session:
        stats = {s.display_name: s for s in await member_completion_stats(session, project_id=project_a.id)}
    s = stats["爱丽丝"]
    assert s.completed_total == 3  # 取消不计入
    assert s.on_time_completed == 2  # 改期后按时 + 无截止
    assert s.on_time_rate == pytest.approx(2 / 3)


async def test_sample_sufficiency_flag(project_a: Project) -> None:
    """n < MIN_SAMPLE_SIZE 标记样本不足；达到阈值不标记；n=0 时率为 None。"""
    _, bob = await add_member(project_a, "bob", "Bob12345!", display_name="鲍勃")
    _, carol = await add_member(project_a, "carol", "Carol123!", display_name="卡罗尔")
    _, dave = await add_member(project_a, "dave", "Dave12345!", display_name="戴夫")
    for i in range(MIN_SAMPLE_SIZE):
        await _add_item(project_a, bob, "COMPLETED", title=f"完成{i}")
    await _add_item(project_a, carol, "COMPLETED", title="唯一一条")

    async with async_session_factory() as session:
        stats = {s.display_name: s for s in await member_completion_stats(session, project_id=project_a.id)}
    assert stats["鲍勃"].sample_sufficient is True
    assert stats["鲍勃"].on_time_rate == 1.0
    assert stats["卡罗尔"].sample_sufficient is False  # 样本不足
    assert stats["戴夫"].completed_total == 0
    assert stats["戴夫"].on_time_rate is None
    assert stats["戴夫"].sample_sufficient is False
