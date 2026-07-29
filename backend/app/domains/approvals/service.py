"""负责人待审批聚合服务（12.6 节）。

聚合 PENDING 的转派申请与 PENDING_APPROVAL 的 DDL 变更申请，统一形状、
按时间倒序返回。权限规则（T3.5 验收）：负责人与管理员（只读）返回数据，
普通成员返回空列表（不 403）。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.approvals.schemas import ApprovalItemOut
from app.domains.collaboration.models import CollaborationRequest
from app.domains.deadlines.models import DeadlineChangeRequest
from app.domains.deadlines.state_machine import DeadlineChangeStatus, DeadlineTargetType
from app.domains.project.models import ROLE_ADMIN, ROLE_LEADER, ProjectMember
from app.domains.transfers.models import TransferRequest
from app.domains.transfers.state_machine import TransferStatus
from app.domains.work_items.models import WorkItem
from app.domains.work_items.schemas import MemberBrief


def _iso_date(value) -> str:
    return value.strftime("%Y-%m-%d") if value is not None else "未设置"


async def list_pending_approvals(
    session: AsyncSession, actor: ProjectMember
) -> list[ApprovalItemOut]:
    """负责人待审批列表；管理员只读同视图；普通成员返回空列表（T3.5 验收，不 403）。"""
    if actor.role not in (ROLE_LEADER, ROLE_ADMIN):
        return []

    transfers = list(
        (
            await session.execute(
                select(TransferRequest).where(
                    TransferRequest.status == TransferStatus.PENDING.value
                )
            )
        )
        .scalars()
        .all()
    )
    deadline_changes = list(
        (
            await session.execute(
                select(DeadlineChangeRequest).where(
                    DeadlineChangeRequest.status == DeadlineChangeStatus.PENDING_APPROVAL.value
                )
            )
        )
        .scalars()
        .all()
    )
    if not transfers and not deadline_changes:
        return []

    # 批量加载关联工作项标题与成员显示名
    item_ids = {r.work_item_id for r in transfers} | {r.work_item_id for r in deadline_changes}
    member_ids: set[uuid.UUID] = set()
    for r in transfers:
        member_ids.update((r.from_member_id, r.to_member_id))
    member_ids.update(r.requested_by for r in deadline_changes)

    item_titles: dict[uuid.UUID, str] = {}
    if item_ids:
        rows = await session.execute(
            select(WorkItem.id, WorkItem.title).where(WorkItem.id.in_(item_ids))
        )
        item_titles = {row.id: row.title for row in rows}

    briefs: dict[uuid.UUID, MemberBrief] = {}
    if member_ids:
        members = (
            (await session.execute(select(ProjectMember).where(ProjectMember.id.in_(member_ids))))
            .scalars()
            .all()
        )
        briefs = {m.id: MemberBrief(id=m.id, display_name=m.display_name) for m in members}

    def brief(member_id: uuid.UUID) -> MemberBrief:
        return briefs.get(member_id) or MemberBrief(id=member_id, display_name="")

    # 协作级 DDL 变更的目标标题（协作请求标题）
    collab_target_ids = {
        r.target_id
        for r in deadline_changes
        if r.target_type == DeadlineTargetType.COLLABORATION_REQUEST
    }
    collab_titles: dict[uuid.UUID, str] = {}
    if collab_target_ids:
        rows = await session.execute(
            select(CollaborationRequest.id, CollaborationRequest.title).where(
                CollaborationRequest.id.in_(collab_target_ids)
            )
        )
        collab_titles = {row.id: row.title for row in rows}

    items: list[ApprovalItemOut] = []
    for r in transfers:
        from_brief = brief(r.from_member_id)
        to_brief = brief(r.to_member_id)
        items.append(
            ApprovalItemOut(
                kind="transfer",
                id=r.id,
                work_item_id=r.work_item_id,
                work_item_title=item_titles.get(r.work_item_id, ""),
                summary=f"{from_brief.display_name} → {to_brief.display_name}",
                requested_by=from_brief,
                status=r.status,
                impact_analysis_status=None,
                version=r.version,
                created_at=r.created_at,
                updated_at=r.updated_at,
                from_member=from_brief,
                to_member=to_brief,
            )
        )
    for r in deadline_changes:
        target_title = (
            item_titles.get(r.work_item_id, "")
            if r.target_type == DeadlineTargetType.WORK_ITEM
            else collab_titles.get(r.target_id, "")
        )
        items.append(
            ApprovalItemOut(
                kind="deadline_change",
                id=r.id,
                work_item_id=r.work_item_id,
                work_item_title=item_titles.get(r.work_item_id, ""),
                summary=(
                    f"{target_title}：{_iso_date(r.old_due_at)} → {_iso_date(r.new_due_at)}"
                ),
                requested_by=brief(r.requested_by),
                status=r.status,
                impact_analysis_status=r.impact_analysis_status,
                version=r.version,
                created_at=r.created_at,
                updated_at=r.updated_at,
                target_type=r.target_type,
                target_id=r.target_id,
                old_due_at=r.old_due_at,
                new_due_at=r.new_due_at,
            )
        )

    items.sort(key=lambda item: item.created_at, reverse=True)
    return items
