"""团队记忆：成员完成数与负载统计（设计文档第 7 节①，M3.1）。

- 只读现有 work_items 数据实时聚合，不另建统计表（最简单方案）；
- 严格项目内口径（已确认决策）：A 项目分配只看成员在 A 项目的历史，
  统计记录不跨项目混入；新项目冷启动无历史属预期；
- 口径：完成数按主执行人（assignee）统计 COMPLETED 工作项；负载为当前
  活跃工作项数（ACTIVE_STATUSES）+ 近 30 天完成数（近期节奏参考）；
- 成员离职/停用（16.7）：统计保留（is_active 透出，由分配侧排除候选）。

2026-08-22 确认：本期不含"各技能标签下完成数"——work_items 无标签字段，
待工作项模型加标签后启用（设计文档待补注）。
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.project.models import ProjectMember
from app.domains.work_items.models import WorkItem
from app.domains.work_items.state_machine import ACTIVE_STATUSES, WorkItemStatus

#: 近期负载窗口（天）
RECENT_DAYS = 30


@dataclass(frozen=True)
class MemberStats:
    """成员在项目内的完成与负载统计（实时聚合结果）。"""

    member_id: uuid.UUID
    display_name: str
    is_active: bool
    completed_total: int
    active_now: int
    completed_recent: int  # 近 RECENT_DAYS 天完成数


async def _count_by_assignee(
    session: AsyncSession, project_id: uuid.UUID, *conditions
) -> dict[uuid.UUID, int]:
    """按主执行人聚合成（成员 id → 数量）。"""
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
    """项目内全体成员的完成数与负载（含停用成员，16.7 统计保留）。"""
    members = (
        await session.execute(
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id)
            .order_by(ProjectMember.display_name)
        )
    ).scalars().all()

    completed_counts = await _count_by_assignee(
        session,
        project_id,
        WorkItem.status == WorkItemStatus.COMPLETED.value,
    )
    active_counts = await _count_by_assignee(
        session, project_id, WorkItem.status.in_(ACTIVE_STATUSES)
    )
    # 近期完成：updated_at 近似完成时间（COMPLETED 后不再有业务更新）
    since = datetime.now(UTC) - timedelta(days=RECENT_DAYS)
    recent_counts = await _count_by_assignee(
        session,
        project_id,
        (WorkItem.status == WorkItemStatus.COMPLETED.value)
        & (WorkItem.updated_at >= since),
    )

    return [
        MemberStats(
            member_id=m.id,
            display_name=m.display_name,
            is_active=m.is_active,
            completed_total=int(completed_counts.get(m.id, 0)),
            active_now=int(active_counts.get(m.id, 0)),
            completed_recent=int(recent_counts.get(m.id, 0)),
        )
        for m in members
    ]
