"""成员完成数与负载统计测试（M3.1 验收，设计文档第 7 节①）。

- 按项目+成员聚合：完成总数（主执行人口径）、当前活跃负载、近 30 天完成数；
- 跨项目数据不混入；停用成员统计保留（16.7）；
- 本期不含技能标签维度（work_items 无标签字段，2026-08-22 确认）。
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from app.domains.memory.member_stats import RECENT_DAYS, member_completion_stats
from app.domains.project.models import Project, ProjectMember
from app.domains.work_items.models import WorkItem
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member, add_member_for_existing_user


async def _add_item(
    project: Project, assignee: ProjectMember, status: str, *, title: str = "任务"
) -> uuid.UUID:
    async with async_session_factory() as session:
        item = WorkItem(
            project_id=project.id, title=title, assignee_id=assignee.id, status=status
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
    """同一用户在两个项目的统计严格分开（按项目分开计算，已确认决策）。"""
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
    """停用成员统计保留、标记停用（16.7），由分配侧排除候选。"""
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
