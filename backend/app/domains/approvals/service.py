"""负责人审批聚合服务（12.6 节）。

- list_pending_approvals：聚合 PENDING 的转派申请、PENDING_APPROVAL 的
  DDL 变更申请与 SUBMITTED 的开发文档（kind="dev_doc"），统一形状、
  按创建时间倒序返回；
- list_processed_approvals：聚合已处理（转派/DDL：APPROVED/REJECTED/CANCELLED；
  开发文档：CONFIRMED/RETURNED）的申请，按 updated_at 倒序、最多 50 条，
  供前端"审批记录"标签页展示谁、什么时候、处理结果。

权限规则（T3.5 验收）：负责人与管理员（只读）返回数据，
普通成员返回空列表（不 403）。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.approvals.schemas import ApprovalItemOut
from app.domains.collaboration.models import CollaborationRequest
from app.domains.deadlines.models import DeadlineChangeRequest
from app.domains.deadlines.state_machine import DeadlineChangeStatus, DeadlineTargetType
from app.domains.dev_docs.models import DevDoc
from app.domains.dev_docs.state_machine import DevDocStatus
from app.domains.project.models import ROLE_ADMIN, ROLE_LEADER, ProjectMember
from app.domains.transfers.models import TransferRequest
from app.domains.transfers.state_machine import TransferStatus
from app.domains.work_items.models import WorkItem
from app.domains.work_items.schemas import MemberBrief

#: 已处理（终态）申请状态：两类申请的状态机枚举一致（8.3/8.4 节）
PROCESSED_TRANSFER_STATUSES = (
    TransferStatus.APPROVED.value,
    TransferStatus.REJECTED.value,
    TransferStatus.CANCELLED.value,
)
PROCESSED_DEADLINE_CHANGE_STATUSES = (
    DeadlineChangeStatus.APPROVED.value,
    DeadlineChangeStatus.REJECTED.value,
    DeadlineChangeStatus.CANCELLED.value,
)
#: 已处理的开发文档状态：确认通过 / 打回（DRAFT 回到成员手中，不算"已处理"）
PROCESSED_DEV_DOC_STATUSES = (
    DevDocStatus.CONFIRMED.value,
    DevDocStatus.RETURNED.value,
)

#: 已处理列表返回的最大条数（审批记录标签页只看最近处理结果）
PROCESSED_LIMIT = 50


def _iso_date(value) -> str:
    return value.strftime("%Y-%m-%d") if value is not None else "未设置"


async def _aggregate_items(
    session: AsyncSession,
    transfers: list[TransferRequest],
    deadline_changes: list[DeadlineChangeRequest],
    dev_docs: list[DevDoc],
) -> list[ApprovalItemOut]:
    """把三类申请聚合成统一形状的审批项（批量加载标题与成员显示名）。"""
    if not transfers and not deadline_changes and not dev_docs:
        return []

    # 批量加载关联工作项标题与成员显示名
    item_ids = (
        {r.work_item_id for r in transfers}
        | {r.work_item_id for r in deadline_changes}
        | {d.work_item_id for d in dev_docs}
    )
    member_ids: set[uuid.UUID] = set()
    for r in transfers:
        member_ids.update((r.from_member_id, r.to_member_id))
        if r.approved_by is not None:
            member_ids.add(r.approved_by)
    for r in deadline_changes:
        member_ids.add(r.requested_by)
        if r.approved_by is not None:
            member_ids.add(r.approved_by)
    for d in dev_docs:
        if d.author_member_id is not None:
            member_ids.add(d.author_member_id)
        if d.confirmed_by is not None:
            member_ids.add(d.confirmed_by)

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

    def approver_brief(member_id: uuid.UUID | None) -> MemberBrief | None:
        return brief(member_id) if member_id is not None else None

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
                approved_by=approver_brief(r.approved_by),
                approved_at=r.approved_at,
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
                approved_by=approver_brief(r.approved_by),
                approved_at=r.approved_at,
                target_type=r.target_type,
                target_id=r.target_id,
                old_due_at=r.old_due_at,
                new_due_at=r.new_due_at,
            )
        )
    for d in dev_docs:
        title = item_titles.get(d.work_item_id, "")
        # 进入审批聚合的文档（SUBMITTED/CONFIRMED/RETURNED）都经过 submit，必有撰写人
        assert d.author_member_id is not None, "已提交的开发文档必有撰写人"
        items.append(
            ApprovalItemOut(
                kind="dev_doc",
                id=d.id,
                work_item_id=d.work_item_id,
                work_item_title=title,
                summary=f"{title}：第 {d.doc_version} 次提交",
                requested_by=brief(d.author_member_id),
                status=d.status,
                impact_analysis_status=None,
                version=d.version,
                created_at=d.created_at,
                updated_at=d.updated_at,
                approved_by=approver_brief(d.confirmed_by),
                approved_at=d.confirmed_at,
                doc_version=d.doc_version,
                review_note=d.review_note,
            )
        )
    return items


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
    dev_docs = list(
        (
            await session.execute(
                select(DevDoc).where(DevDoc.status == DevDocStatus.SUBMITTED.value)
            )
        )
        .scalars()
        .all()
    )

    items = await _aggregate_items(session, transfers, deadline_changes, dev_docs)
    items.sort(key=lambda item: item.created_at, reverse=True)
    return items


async def list_processed_approvals(
    session: AsyncSession, actor: ProjectMember, *, limit: int = PROCESSED_LIMIT
) -> list[ApprovalItemOut]:
    """已处理审批记录：终态（APPROVED/REJECTED/CANCELLED）申请按 updated_at
    倒序、最多 limit 条；权限与待审批列表一致（普通成员空列表，不 403）。"""
    if actor.role not in (ROLE_LEADER, ROLE_ADMIN):
        return []

    transfers = list(
        (
            await session.execute(
                select(TransferRequest)
                .where(TransferRequest.status.in_(PROCESSED_TRANSFER_STATUSES))
                .order_by(TransferRequest.updated_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    deadline_changes = list(
        (
            await session.execute(
                select(DeadlineChangeRequest)
                .where(DeadlineChangeRequest.status.in_(PROCESSED_DEADLINE_CHANGE_STATUSES))
                .order_by(DeadlineChangeRequest.updated_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    dev_docs = list(
        (
            await session.execute(
                select(DevDoc)
                .where(DevDoc.status.in_(PROCESSED_DEV_DOC_STATUSES))
                .order_by(DevDoc.updated_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    items = await _aggregate_items(session, transfers, deadline_changes, dev_docs)
    items.sort(key=lambda item: item.updated_at, reverse=True)
    return items[:limit]
