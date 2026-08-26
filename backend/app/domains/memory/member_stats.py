"""团队记忆：成员完成数、负载与按时完成率统计（设计文档第 7 节①，M3.1/M3.2）。

- 只读现有 work_items 数据实时聚合，不另建统计表（最简单方案）；
- 严格项目内口径（已确认决策）：A 项目分配只看成员在 A 项目的历史，
  统计记录不跨项目混入；新项目冷启动无历史属预期；
- 完成数按主执行人（assignee）统计 COMPLETED 工作项；负载为当前
  活跃工作项数（ACTIVE_STATUSES）+ 近 30 天完成数（近期节奏参考）；
- 按时完成率（16.8 宽口径，M3.2）：分子为"不晚于最终截止时间完成"的已完成项
  （due_at 即最终截止时间——DDL 变更批准后已就地更新）；分母仅已完成项，
  取消/转交不计入；无截止时间的已完成项视为按时（无约可逾）；
  展示附样本量 n，n < MIN_SAMPLE_SIZE 标记"样本不足"；
  完成时间用 updated_at 近似（COMPLETED 后不再有业务更新）；
- 成员离职/停用（16.7）：统计保留（is_active 透出，由分配侧排除候选）。

2026-08-22 确认：本期不含"各技能标签下完成数"——work_items 无标签字段，
待工作项模型加标签后启用（设计文档第 16 节第 14 条）。
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

#: 样本量阈值（16.8）：低于此值标记"样本不足"
MIN_SAMPLE_SIZE = 5


@dataclass(frozen=True)
class MemberStats:
    """成员在项目内的完成、负载与按时完成率统计（实时聚合结果）。"""

    member_id: uuid.UUID
    display_name: str
    is_active: bool
    completed_total: int
    active_now: int
    completed_recent: int  # 近 RECENT_DAYS 天完成数
    on_time_completed: int  # 不晚于最终截止时间完成的项数（分子）
    sample_sufficient: bool  # completed_total >= MIN_SAMPLE_SIZE

    @property
    def on_time_rate(self) -> float | None:
        """按时完成率；无已完成样本时为 None（前端展示"暂无数据"）。"""
        if self.completed_total == 0:
            return None
        return self.on_time_completed / self.completed_total


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
    """项目内全体成员的完成数、负载与按时完成率（含停用成员，16.7 统计保留）。"""
    members = (
        await session.execute(
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id)
            .order_by(ProjectMember.display_name)
        )
    ).scalars().all()

    # 已完成项取行级数据：一次查询同时支撑完成总数、近期完成与按时率
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
        # 宽口径（16.8）：不晚于最终截止时间；无截止时间的视为按时（无约可逾）
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
