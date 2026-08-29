"""负责人审批聚合服务。

`list_pending_approvals` 聚合待处理的转派、DDL 变更和开发文档。
`list_processed_approvals` 聚合终态申请与交付审核结论，并按更新时间返回最近记录。

普通成员访问时返回空列表而非 `403`。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.approvals.schemas import ApprovalItemOut
from app.domains.collaboration.models import CollaborationRequest
from app.domains.deadlines.models import DeadlineChangeRequest
from app.domains.deadlines.state_machine import DeadlineChangeStatus, DeadlineTargetType
from app.domains.deliverables.models import Deliverable
from app.domains.dev_docs.models import DevDoc
from app.domains.dev_docs.state_machine import DevDocStatus
from app.domains.project.models import ROLE_LEADER, ProjectMember
from app.domains.reviews.models import Review
from app.domains.transfers.models import TransferRequest
from app.domains.transfers.state_machine import TransferStatus
from app.domains.work_items.models import WorkItem
from app.domains.work_items.schemas import MemberBrief

#: 转派申请的终态。
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
#: 开发文档确认或打回后计入已处理记录；重新编辑的 `DRAFT` 不计入。
PROCESSED_DEV_DOC_STATUSES = (
    DevDocStatus.CONFIRMED.value,
    DevDocStatus.RETURNED.value,
)

#: 已处理列表只保留最近结果。
PROCESSED_LIMIT = 50


def _iso_date(value) -> str:
    return value.strftime("%Y-%m-%d") if value is not None else "未设置"


async def _aggregate_items(
    session: AsyncSession,
    transfers: list[TransferRequest],
    deadline_changes: list[DeadlineChangeRequest],
    dev_docs: list[DevDoc],
    reviews: list[Review] | None = None,
) -> list[ApprovalItemOut]:
    """把申请与交付审核结论聚合成统一形状的审批项（批量加载标题与成员显示名）。"""
    reviews = reviews or []
    if not transfers and not deadline_changes and not dev_docs and not reviews:
        return []

    # 批量加载关联数据，避免为每条审批项单独查询。
    item_ids = (
        {r.work_item_id for r in transfers}
        | {r.work_item_id for r in deadline_changes}
        | {d.work_item_id for d in dev_docs}
        | {r.work_item_id for r in reviews}
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
    for review in reviews:
        member_ids.add(review.reviewed_by)

    deliverables: dict[uuid.UUID, Deliverable] = {}
    if reviews:
        del_rows = (
            (
                await session.execute(
                    select(Deliverable).where(
                        Deliverable.id.in_({review.deliverable_id for review in reviews})
                    )
                )
            )
            .scalars()
            .all()
        )
        deliverables = {d.id: d for d in del_rows}
        member_ids.update(d.submitted_by for d in del_rows)

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
        # 这些状态都必须经过 `submit`，因此撰写人不能为空。
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
    for review in reviews:
        title = item_titles.get(review.work_item_id, "")
        deliverable = deliverables.get(review.deliverable_id)
        # 交付物通常与审核记录同时存在；异常缺失时降级展示，避免阻塞整个列表。
        items.append(
            ApprovalItemOut(
                kind="delivery_review",
                id=review.id,
                work_item_id=review.work_item_id,
                work_item_title=title,
                summary=(
                    f"{title}：第 {deliverable.version} 版交付审核"
                    if deliverable is not None
                    else f"{title}：交付审核"
                ),
                requested_by=(
                    brief(deliverable.submitted_by)
                    if deliverable is not None
                    else brief(review.reviewed_by)
                ),
                status=review.decision,
                impact_analysis_status=None,
                version=1,
                created_at=review.created_at,
                updated_at=review.updated_at,
                approved_by=approver_brief(review.reviewed_by),
                approved_at=review.created_at,
                deliverable_version=deliverable.version if deliverable else None,
                deliverable_type=deliverable.type if deliverable else None,
            )
        )
    return items


async def list_pending_approvals(
    session: AsyncSession, actor: ProjectMember
) -> list[ApprovalItemOut]:
    """返回负责人待审批列表；普通成员返回空列表而非 `403`。"""
    if actor.role not in (ROLE_LEADER):
        return []

    # 申请不冗余 `project_id`，必须通过所属工作项实施项目隔离。
    transfers = list(
        (
            await session.execute(
                select(TransferRequest)
                .join(WorkItem, WorkItem.id == TransferRequest.work_item_id)
                .where(
                    WorkItem.project_id == actor.project_id,
                    TransferRequest.status == TransferStatus.PENDING.value,
                )
            )
        )
        .scalars()
        .all()
    )
    deadline_changes = list(
        (
            await session.execute(
                select(DeadlineChangeRequest)
                .join(WorkItem, WorkItem.id == DeadlineChangeRequest.work_item_id)
                .where(
                    WorkItem.project_id == actor.project_id,
                    DeadlineChangeRequest.status == DeadlineChangeStatus.PENDING_APPROVAL.value,
                )
            )
        )
        .scalars()
        .all()
    )
    dev_docs = list(
        (
            await session.execute(
                select(DevDoc)
                .join(WorkItem, WorkItem.id == DevDoc.work_item_id)
                .where(
                    WorkItem.project_id == actor.project_id,
                    DevDoc.status == DevDocStatus.SUBMITTED.value,
                )
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
    """按 `updated_at` 倒序返回最多 `limit` 条已处理记录。

    权限与待审批列表一致，普通成员返回空列表而非 `403`。
    """
    if actor.role not in (ROLE_LEADER):
        return []

    # 申请与审核均通过所属工作项实施项目隔离。
    transfers = list(
        (
            await session.execute(
                select(TransferRequest)
                .join(WorkItem, WorkItem.id == TransferRequest.work_item_id)
                .where(
                    WorkItem.project_id == actor.project_id,
                    TransferRequest.status.in_(PROCESSED_TRANSFER_STATUSES),
                )
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
                .join(WorkItem, WorkItem.id == DeadlineChangeRequest.work_item_id)
                .where(
                    WorkItem.project_id == actor.project_id,
                    DeadlineChangeRequest.status.in_(PROCESSED_DEADLINE_CHANGE_STATUSES),
                )
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
                .join(WorkItem, WorkItem.id == DevDoc.work_item_id)
                .where(
                    WorkItem.project_id == actor.project_id,
                    DevDoc.status.in_(PROCESSED_DEV_DOC_STATUSES),
                )
                .order_by(DevDoc.updated_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    # 每条 `reviews` 记录都是已落定的终审结论。
    reviews = list(
        (
            await session.execute(
                select(Review)
                .join(WorkItem, WorkItem.id == Review.work_item_id)
                .where(WorkItem.project_id == actor.project_id)
                .order_by(Review.updated_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    items = await _aggregate_items(session, transfers, deadline_changes, dev_docs, reviews)
    items.sort(key=lambda item: item.updated_at, reverse=True)
    return items[:limit]
