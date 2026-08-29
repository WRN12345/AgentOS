"""按项目实时统计成员完成数、负载和按时完成率。

统计直接聚合 `work_items`，严格按项目隔离。完成数按主执行人统计已完成工作项；
负载包含当前活跃工作项数和近期完成数。按时率以不晚于 `due_at` 完成为准，
无截止时间视为按时，完成时间以进入终态后的 `updated_at` 近似。停用成员仍保留历史统计，
由分配侧排除候选。当前工作项没有技能标签，因此不提供按技能分类的完成数。
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.project.models import ProjectMember
from app.domains.work_items.models import WorkItem
from app.domains.work_items.state_machine import ACTIVE_STATUSES, WorkItemStatus

RECENT_DAYS = 30

#: 低于此完成样本数时标记为样本不足
MIN_SAMPLE_SIZE = 5


@dataclass(frozen=True)
class MemberStats:
    """成员在项目内的完成、负载与按时完成率统计（实时聚合结果）。"""

    member_id: uuid.UUID
    display_name: str
    is_active: bool
    completed_total: int
    active_now: int
    completed_recent: int
    on_time_completed: int
    sample_sufficient: bool

    @property
    def on_time_rate(self) -> float | None:
        """返回按时完成率；没有已完成样本时返回 `None`。"""
        if self.completed_total == 0:
            return None
        return self.on_time_completed / self.completed_total


async def _count_by_assignee(
    session: AsyncSession, project_id: uuid.UUID, *conditions
) -> dict[uuid.UUID, int]:
    """按主执行人聚合工作项数量。"""
    rows = (
        await session.execute(
            select(WorkItem.assignee_id, func.count())
            .where(WorkItem.project_id == project_id, *conditions)
            .group_by(WorkItem.assignee_id)
        )
    ).all()
    return {assignee_id: int(count) for assignee_id, count in rows}


async def member_completion_stats(
    session: AsyncSession, *, project_id: uuid.UUID
) -> list[MemberStats]:
    """返回项目全体成员的完成数、负载和按时完成率，包含停用成员。"""
    members = (
        await session.execute(
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id)
            .order_by(ProjectMember.display_name)
        )
    ).scalars().all()

    # 一次读取已完成项，同时计算总数、近期完成数和按时率
    completed_rows = (
        await session.execute(
            select(WorkItem.assignee_id, WorkItem.due_at, WorkItem.updated_at).where(
                WorkItem.project_id == project_id,
                WorkItem.status == WorkItemStatus.COMPLETED.value,
            )
        )
    ).all()
    active_counts = await _count_by_assignee(
        session, project_id, WorkItem.status.in_(ACTIVE_STATUSES)
    )

    since = datetime.now(UTC) - timedelta(days=RECENT_DAYS)
    by_member: dict[uuid.UUID, dict[str, int]] = {}
    for assignee_id, due_at, updated_at in completed_rows:
        bucket = by_member.setdefault(
            assignee_id, {"total": 0, "recent": 0, "on_time": 0}
        )
        bucket["total"] += 1
        if updated_at >= since:
            bucket["recent"] += 1
        # 无截止时间时不存在逾期约束，按时计入
        if due_at is None or updated_at <= due_at:
            bucket["on_time"] += 1

    return [
        MemberStats(
            member_id=m.id,
            display_name=m.display_name,
            is_active=m.is_active,
            completed_total=by_member.get(m.id, {}).get("total", 0),
            active_now=int(active_counts.get(m.id, 0)),
            completed_recent=by_member.get(m.id, {}).get("recent", 0),
            on_time_completed=by_member.get(m.id, {}).get("on_time", 0),
            sample_sufficient=by_member.get(m.id, {}).get("total", 0) >= MIN_SAMPLE_SIZE,
        )
        for m in members
    ]
