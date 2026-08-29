"""交付物应用服务与权限策略。

仅工作项当前主执行人可提交，终态工作项拒绝新交付物。版本号取当前最大值加一，
`(work_item_id, version)` 唯一约束负责阻止并发重提，冲突返回
`409 DELIVERABLE_VERSION_CONFLICT`。`file` 类型必须引用同项目且可关联到当前工作项
的文件。每次提交与 `deliverable.submitted` 审计事件在同一事务内写入。
"""

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiException, ErrorCodes
from app.core.logging import setup_logging
from app.domains.audit.service import record_event
from app.domains.collaboration.models import CollaborationRequest
from app.domains.deliverables.models import Deliverable
from app.domains.deliverables.schemas import (
    DeliverableCreateIn,
    DeliverableListItemOut,
    DeliverableOut,
    DeliverableReviewBrief,
    FileBrief,
)
from app.domains.files.models import StoredFile
from app.domains.files.service import get_stored_file, is_work_item_related
from app.domains.project.models import ROLE_LEADER, ProjectMember
from app.domains.reviews.models import Review
from app.domains.work_items.models import WorkItem, WorkItemCollaborator
from app.domains.work_items.schemas import MemberBrief
from app.domains.work_items.service import get_work_item
from app.domains.work_items.state_machine import WorkItemStatus

logger = setup_logging("backend")




async def get_deliverable(
    session: AsyncSession, deliverable_id: uuid.UUID, *, project_id: uuid.UUID | None = None
) -> Deliverable:
    deliverable = await session.get(Deliverable, deliverable_id)
    if deliverable is None or (project_id is not None and deliverable.project_id != project_id):
        # 将跨项目访问视为资源不存在，避免泄露资源存在性。
        raise ApiException(404, ErrorCodes.NOT_FOUND, "交付物不存在")
    return deliverable


async def has_deliverable(session: AsyncSession, work_item_id: uuid.UUID) -> bool:
    """检查工作项是否已有交付物，供提交审核前置校验使用。"""
    row = (
        await session.execute(
            select(Deliverable.id).where(Deliverable.work_item_id == work_item_id).limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def _to_out(session: AsyncSession, deliverable: Deliverable) -> DeliverableOut:
    submitter = await session.get(ProjectMember, deliverable.submitted_by)
    file_brief: FileBrief | None = None
    if deliverable.stored_file_id is not None:
        stored = await session.get(StoredFile, deliverable.stored_file_id)
        if stored is not None:
            file_brief = FileBrief(
                id=stored.id,
                original_filename=stored.original_filename,
                size_bytes=stored.size_bytes,
                mime_type=stored.mime_type,
                sha256=stored.sha256,
            )
    return DeliverableOut(
        id=deliverable.id,
        work_item_id=deliverable.work_item_id,
        type=deliverable.type,
        content=deliverable.content,
        file=file_brief,
        version=deliverable.version,
        submitted_by=MemberBrief(
            id=deliverable.submitted_by,
            display_name=submitter.display_name if submitter else "",
        ),
        created_at=deliverable.created_at,
        updated_at=deliverable.updated_at,
    )


async def _check_visible(session: AsyncSession, actor: ProjectMember, item_id: uuid.UUID) -> None:
    """仅负责人或工作项相关成员可见交付物。"""
    if actor.role in (ROLE_LEADER):
        return
    if not await is_work_item_related(session, item_id, actor.id):
        raise ApiException(403, ErrorCodes.FORBIDDEN, "无权查看该工作项的交付物")


async def list_deliverables(
    session: AsyncSession, actor: ProjectMember, item_id: uuid.UUID
) -> list[DeliverableOut]:
    """按 `version` 倒序返回保留的全部版本。"""
    await get_work_item(session, item_id, project_id=actor.project_id)  # 越权 → 404
    await _check_visible(session, actor, item_id)
    deliverables = list(
        (
            await session.execute(
                select(Deliverable)
                .where(Deliverable.work_item_id == item_id)
                .order_by(Deliverable.version.desc())
            )
        )
        .scalars()
        .all()
    )
    return [await _to_out(session, d) for d in deliverables]


async def _to_list_items(
    session: AsyncSession, actor: ProjectMember, deliverables: list[Deliverable]
) -> list[DeliverableListItemOut]:
    """批量序列化交付物列表项（标题、提交人、审核结论）。

    审核反馈仅负责人、任务当前主执行人和提交人可见，其他可见成员只能查看结论。
    """
    if not deliverables:
        return []

    item_ids = {d.work_item_id for d in deliverables}
    rows = await session.execute(
        select(WorkItem.id, WorkItem.title, WorkItem.assignee_id).where(
            WorkItem.id.in_(item_ids)
        )
    )
    items_meta = {row.id: row for row in rows}

    reviews = list(
        (
            await session.execute(
                select(Review).where(
                    Review.deliverable_id.in_({d.id for d in deliverables})
                )
            )
        )
        .scalars()
        .all()
    )
    review_by_deliverable = {r.deliverable_id: r for r in reviews}

    member_ids = {d.submitted_by for d in deliverables} | {
        r.reviewed_by for r in reviews
    }
    members = (
        (
            await session.execute(
                select(ProjectMember).where(ProjectMember.id.in_(member_ids))
            )
        )
        .scalars()
        .all()
    )
    briefs = {m.id: MemberBrief(id=m.id, display_name=m.display_name) for m in members}

    def brief(member_id: uuid.UUID) -> MemberBrief:
        return briefs.get(member_id) or MemberBrief(id=member_id, display_name="")

    result: list[DeliverableListItemOut] = []
    for d in deliverables:
        meta = items_meta.get(d.work_item_id)
        can_see_feedback = (
            actor.role == ROLE_LEADER
            or actor.id == d.submitted_by
            or (meta is not None and meta.assignee_id == actor.id)
        )
        review = review_by_deliverable.get(d.id)
        review_brief: DeliverableReviewBrief | None = None
        if review is not None:
            review_brief = DeliverableReviewBrief(
                decision=review.decision,
                feedback=review.feedback if can_see_feedback else None,
                reviewed_by=brief(review.reviewed_by),
                created_at=review.created_at,
            )
        result.append(
            DeliverableListItemOut(
                id=d.id,
                work_item_id=d.work_item_id,
                work_item_title=meta.title if meta else "",
                type=d.type,
                version=d.version,
                submitted_by=brief(d.submitted_by),
                created_at=d.created_at,
                review=review_brief,
            )
        )
    return result


async def list_mine(
    session: AsyncSession, actor: ProjectMember
) -> list[DeliverableListItemOut]:
    """我提交的交付物（提交时间倒序）及审核结论，供审批中心"我的申请"页展示。"""
    deliverables = list(
        (
            await session.execute(
                select(Deliverable)
                .where(
                    Deliverable.project_id == actor.project_id,
                    Deliverable.submitted_by == actor.id,
                )
                .order_by(Deliverable.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return await _to_list_items(session, actor, deliverables)


async def list_visible(
    session: AsyncSession, actor: ProjectMember
) -> list[DeliverableListItemOut]:
    """按提交时间倒序返回可见交付物；普通成员仅能查看相关工作项。"""
    stmt = (
        select(Deliverable)
        .where(Deliverable.project_id == actor.project_id)
        .order_by(Deliverable.created_at.desc())
    )
    if actor.role not in (ROLE_LEADER):
        related_item_ids = select(WorkItem.id).where(
            or_(
                WorkItem.assignee_id == actor.id,
                WorkItem.id.in_(
                    select(WorkItemCollaborator.work_item_id).where(
                        WorkItemCollaborator.member_id == actor.id
                    )
                ),
                WorkItem.id.in_(
                    select(CollaborationRequest.work_item_id).where(
                        or_(
                            CollaborationRequest.requester_id == actor.id,
                            CollaborationRequest.assignee_id == actor.id,
                        )
                    )
                ),
            )
        )
        stmt = stmt.where(Deliverable.work_item_id.in_(related_item_ids))
    deliverables = list((await session.execute(stmt)).scalars().all())
    return await _to_list_items(session, actor, deliverables)


async def get_deliverable_version(
    session: AsyncSession, actor: ProjectMember, item_id: uuid.UUID, version: int
) -> DeliverableOut:
    """按版本号查询单条交付物。"""
    await get_work_item(session, item_id, project_id=actor.project_id)  # 越权 → 404
    await _check_visible(session, actor, item_id)
    deliverable = (
        await session.execute(
            select(Deliverable).where(
                Deliverable.work_item_id == item_id, Deliverable.version == version
            )
        )
    ).scalar_one_or_none()
    if deliverable is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "交付物版本不存在")
    return await _to_out(session, deliverable)




async def validate_file_reference(
    session: AsyncSession, item_id: uuid.UUID, file_id: uuid.UUID
) -> StoredFile:
    """校验 `file` 引用：文件存在、归属同一工作项
    （未关联则建立关联）、上传人与工作项有关，并且文件与工作项属于同一项目。
    跨项目文件按 `404` 处理，避免泄露存在性。
    """
    item = await session.get(WorkItem, item_id)
    if item is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "工作项不存在")
    stored = await get_stored_file(session, file_id, project_id=item.project_id)  # 他项目文件 → 404
    if stored.work_item_id is not None and stored.work_item_id != item_id:
        raise ApiException(
            422,
            ErrorCodes.VALIDATION_ERROR,
            "文件已关联其他工作项，不能作为本工作项交付物",
            {"file_id": str(file_id)},
        )
    uploader = await session.get(ProjectMember, stored.uploaded_by)
    uploader_allowed = (uploader is not None and uploader.role == ROLE_LEADER) or (
        await is_work_item_related(session, item_id, stored.uploaded_by)
    )
    if not uploader_allowed:
        raise ApiException(
            403, ErrorCodes.FORBIDDEN, "文件上传人与该工作项无关，不能引用为交付物"
        )
    if stored.work_item_id is None:
        stored.work_item_id = item_id  # 与交付物在同一事务内建立关联。
    return stored


async def create_deliverable(
    session: AsyncSession, actor: ProjectMember, item_id: uuid.UUID, payload: DeliverableCreateIn
) -> DeliverableOut:
    """由当前主执行人提交新版本；终态工作项拒绝提交并记录审计。"""
    item = await get_work_item(session, item_id, project_id=actor.project_id)  # 越权 → 404
    if item.assignee_id != actor.id:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅工作项当前主执行人可提交交付物")
    if item.status in (WorkItemStatus.COMPLETED.value, WorkItemStatus.CANCELLED.value):
        raise ApiException(
            409,
            ErrorCodes.WORK_ITEM_INVALID_TRANSITION,
            f"工作项已处于终态 {item.status}，不可再提交交付物",
            {"current_status": item.status},
        )

    stored: StoredFile | None = None
    if payload.type == "file":
        stored = await validate_file_reference(session, item.id, payload.file_id)  # type: ignore[arg-type]

    max_version = (
        await session.execute(
            select(func.max(Deliverable.version)).where(Deliverable.work_item_id == item.id)
        )
    ).scalar_one()
    deliverable = Deliverable(
        # 项目归属从已校验的工作项推导，不信任客户端输入。
        project_id=item.project_id,
        work_item_id=item.id,
        type=payload.type,
        content=payload.content,
        stored_file_id=stored.id if stored else None,
        version=(max_version or 0) + 1,
        submitted_by=actor.id,
    )
    session.add(deliverable)
    try:
        await session.flush()
    except IntegrityError:
        # 唯一约束阻止并发请求写入相同版本号。
        await session.rollback()
        raise ApiException(
            409,
            ErrorCodes.DELIVERABLE_VERSION_CONFLICT,
            "交付物版本冲突，请刷新后重试",
        ) from None

    after: dict[str, object] = {
        "work_item_id": str(item.id),
        "type": deliverable.type,
        "version": deliverable.version,
    }
    if deliverable.type == "file" and stored is not None:
        after["stored_file_id"] = str(stored.id)
        after["sha256"] = stored.sha256
    else:
        # 审计仅保留文本或链接摘要，避免复制完整内容。
        after["content_preview"] = (deliverable.content or "")[:200]
    await record_event(
        session,
        actor_id=actor.user_id,
        action="deliverable.submitted",
        target_type="deliverable",
        target_id=deliverable.id,
        before=None,
        after=after,
    )
    await session.commit()
    await session.refresh(deliverable)  # created_at/updated_at 由数据库生成，刷新取回
    logger.info(
        "deliverable submitted: id=%s, work_item_id=%s, version=%d",
        deliverable.id,
        item.id,
        deliverable.version,
    )
    return await _to_out(session, deliverable)
