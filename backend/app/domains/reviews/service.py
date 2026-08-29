"""最终审核应用服务与权限策略。

- 仅项目负责人可审核，且工作项必须处于 IN_REVIEW；
- approve 将状态改为 COMPLETED，request_changes 将状态改为 IN_PROGRESS 且必须填写反馈，
  reject 拒绝当前交付但保持 IN_REVIEW；
- 审核记录、状态与 version、审计事件和通知在同一事务写入，commit 后发布 SSE；
- 通知不含反馈正文，反馈仅对项目负责人与工作项主执行人可见；
- 工作项完成后以 best-effort 方式异步投递结论索引和经验总结，不影响审核事务。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiException, ErrorCodes
from app.core.logging import setup_logging
from app.domains.audit.service import record_event
from app.domains.deliverables.models import Deliverable
from app.domains.deliverables.service import get_deliverable
from app.domains.memory.history import enqueue_work_item_conclusion_index
from app.domains.memory.summary import enqueue_work_item_summary
from app.domains.notifications.service import notify
from app.domains.project.models import ROLE_LEADER, ProjectMember
from app.domains.reviews.models import Review
from app.domains.reviews.schemas import ReviewCreateIn, ReviewOut
from app.domains.work_items.schemas import MemberBrief
from app.domains.work_items.service import get_work_item
from app.domains.work_items.state_machine import WorkItemStatus, transition
from app.infrastructure.events import OutgoingEvent, publish_after_commit

logger = setup_logging("backend")

# None 表示 reject 不改变工作项状态
_DECISION_COMMAND = {
    "approve": "complete",
    "request_changes": "request_changes",
    "reject": None,
}

_DECISION_ACTION = {
    "approve": "review.approved",
    "request_changes": "review.changes_requested",
    "reject": "review.rejected",
}

_DECISION_NOTIFY_TITLE = {
    "approve": "工作项审核通过",
    "request_changes": "工作项需要修改",
    "reject": "交付被拒绝",
}


# ---------- 查询与序列化 ----------


async def _to_out(session: AsyncSession, review: Review, work_item_status: str) -> ReviewOut:
    reviewer = await session.get(ProjectMember, review.reviewed_by)
    deliverable = await session.get(Deliverable, review.deliverable_id)
    return ReviewOut(
        id=review.id,
        work_item_id=review.work_item_id,
        deliverable_id=review.deliverable_id,
        deliverable_version=deliverable.version if deliverable else 0,
        decision=review.decision,
        feedback=review.feedback,
        reviewed_by=MemberBrief(
            id=review.reviewed_by, display_name=reviewer.display_name if reviewer else ""
        ),
        work_item_status=work_item_status,
        created_at=review.created_at,
        updated_at=review.updated_at,
    )


def _check_feedback_visible(actor: ProjectMember, item_assignee_id: uuid.UUID) -> None:
    """仅允许项目负责人与工作项主执行人查看反馈正文。"""
    if actor.role != ROLE_LEADER and actor.id != item_assignee_id:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "审核反馈仅负责人与该工作项主执行人可见")


async def list_reviews(
    session: AsyncSession, actor: ProjectMember, item_id: uuid.UUID
) -> list[ReviewOut]:
    """返回工作项审核记录；反馈正文仅对负责人与主执行人可见。"""
    item = await get_work_item(session, item_id, project_id=actor.project_id)  # 越权按不存在处理
    _check_feedback_visible(actor, item.assignee_id)
    reviews = list(
        (
            await session.execute(
                select(Review)
                .where(Review.work_item_id == item_id)
                .order_by(Review.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [await _to_out(session, r, item.status) for r in reviews]


# ---------- 用例 ----------


async def create_review(
    session: AsyncSession, actor: ProjectMember, item_id: uuid.UUID, payload: ReviewCreateIn
) -> ReviewOut:
    """由项目负责人审核，并在同一事务中记录状态、审核、审计和通知。"""
    if actor.role != ROLE_LEADER:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅项目负责人可审核")
    item = await get_work_item(session, item_id, project_id=actor.project_id)  # 越权按不存在处理
    if item.status != WorkItemStatus.IN_REVIEW.value:
        raise ApiException(
            409,
            ErrorCodes.WORK_ITEM_INVALID_TRANSITION,
            f"当前状态 {item.status} 不允许审核，工作项须处于 IN_REVIEW",
            {"current_status": item.status},
        )

    deliverable = await get_deliverable(session, payload.deliverable_id, project_id=actor.project_id)
    if deliverable.work_item_id != item.id:
        raise ApiException(
            422,
            ErrorCodes.VALIDATION_ERROR,
            "交付物不属于该工作项",
            {"deliverable_id": str(payload.deliverable_id)},
        )

    # reject 不触发状态迁移，工作项保持 IN_REVIEW
    events: list[OutgoingEvent] = []
    before_status = item.status
    command = _DECISION_COMMAND[payload.decision]
    if command is not None:
        item.status = transition(item.status, command).value
        item.version += 1

    review = Review(
        work_item_id=item.id,
        deliverable_id=deliverable.id,
        decision=payload.decision,
        feedback=payload.feedback,
        reviewed_by=actor.id,
    )
    session.add(review)
    await session.flush()

    action = _DECISION_ACTION[payload.decision]
    after: dict[str, object] = {
        "status": item.status,
        "decision": payload.decision,
        "deliverable_id": str(deliverable.id),
        "deliverable_version": deliverable.version,
    }
    if payload.feedback:
        after["feedback"] = payload.feedback  # 反馈仅写入审计，不进入通知正文
    await record_event(
        session,
        actor_id=actor.user_id,
        action=action,
        target_type="work_item",
        target_id=item.id,
        before={"status": before_status},
        after=after,
    )

    # 通知与审核同事务写入，正文不含受限的反馈信息
    await notify(
        session,
        project_id=actor.project_id,
        recipient_id=item.assignee_id,
        type=action,
        title=_DECISION_NOTIFY_TITLE[payload.decision],
        body=(
            f"{actor.display_name} 审核了工作项「{item.title}」的交付物"
            f"（第 {deliverable.version} 版）：{payload.decision}"
        ),
        link=f"/work-items/{item.id}",
        outbox=events,
    )
    await session.commit()
    await publish_after_commit(events)

    # 派生任务采用 best-effort，不扩大审核事务，也不阻塞审核结果
    if item.status == WorkItemStatus.COMPLETED.value:
        await enqueue_work_item_conclusion_index(item)
        await enqueue_work_item_summary(item)

    await session.refresh(review)
    logger.info(
        "review created: id=%s, work_item_id=%s, decision=%s, %s -> %s",
        review.id,
        item.id,
        payload.decision,
        before_status,
        item.status,
    )
    return await _to_out(session, review, item.status)
