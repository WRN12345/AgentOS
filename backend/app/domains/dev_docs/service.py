"""开发文档应用服务与权限策略。

每个用例显式校验权限：
- 撰写、编辑和 `submit` 仅工作项当前主执行人可执行；
- `confirm`、`return` 和 `waive` 仅通过 `get_current_leader` 校验的负责人可执行；
- 项目成员可查询文档。

核心约束：
- 每个工作项一份文档，由 `work_item_id` 唯一约束保证；`DRAFT` 和 `RETURNED`
  可编辑，`SUBMITTED` 和 `CONFIRMED` 只读；
- `submit` 要求内容非空并递增 `doc_version`，业务事务 `commit` 后尽力触发
  `dev_doc_review` Agent 初审，投递失败不影响已完成的提交；
- `waive` 是独立标记：负责人可免除文档要求，但不改变状态机；
- 每次状态或内容变更与审计事件在同一事务内写入。
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.models import AgentRun, AgentSuggestion
from app.agents.service import request_agent_analysis
from app.agents.specialists.dev_doc_review import (
    AGENT_TYPE as DEV_DOC_REVIEW_AGENT_TYPE,
    SUGGESTION_TYPE as DEV_DOC_REVIEW_SUGGESTION_TYPE,
)
from app.core.errors import ApiException, ErrorCodes
from app.core.logging import setup_logging
from app.domains.audit.service import record_event
from app.domains.dev_docs.models import DevDoc
from app.domains.dev_docs.schemas import DevDocOut, DevDocUpdateIn
from app.domains.dev_docs.state_machine import DevDocStatus, transition
from app.domains.project.models import ProjectMember
from app.domains.work_items.models import WorkItem
from app.domains.work_items.schemas import MemberBrief
from app.domains.work_items.service import get_work_item
from app.infrastructure.cache.redis import create_redis_client

logger = setup_logging("backend")




async def get_doc(
    session: AsyncSession, work_item_id: uuid.UUID, *, for_update: bool = False
) -> DevDoc | None:
    """按工作项获取文档；写路径通过 `for_update` 持有行锁。"""
    stmt = select(DevDoc).where(DevDoc.work_item_id == work_item_id)
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


def _check_version(doc: DevDoc, version: int) -> None:
    """校验乐观锁版本，不匹配时返回 `409`。"""
    if doc.version != version:
        raise ApiException(
            409,
            ErrorCodes.DEV_DOC_VERSION_CONFLICT,
            "文档已被他人修改，请刷新后重试",
            details={"current_version": doc.version},
        )


def _require_assignee(actor: ProjectMember, item: WorkItem) -> None:
    if item.assignee_id != actor.id:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅当前主执行人可撰写/提交开发文档")


async def _latest_review_suggestion_id(
    session: AsyncSession, work_item_id: uuid.UUID
) -> uuid.UUID | None:
    """返回最近一次 `dev_doc_review` 建议 ID；LLM 不可用时返回 `None`。"""
    return (
        await session.execute(
            select(AgentSuggestion.id)
            .join(AgentRun, AgentRun.id == AgentSuggestion.run_id)
            .where(
                AgentRun.work_item_id == work_item_id,
                AgentSuggestion.suggestion_type == DEV_DOC_REVIEW_SUGGESTION_TYPE,
            )
            .order_by(AgentSuggestion.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _to_out(session: AsyncSession, doc: DevDoc) -> DevDocOut:
    item = await session.get(WorkItem, doc.work_item_id)
    member_ids = {
        mid for mid in (doc.author_member_id, doc.confirmed_by) if mid is not None
    }
    briefs: dict[uuid.UUID, MemberBrief] = {}
    if member_ids:
        members = (
            (await session.execute(select(ProjectMember).where(ProjectMember.id.in_(member_ids))))
            .scalars()
            .all()
        )
        briefs = {m.id: MemberBrief(id=m.id, display_name=m.display_name) for m in members}

    def brief(member_id: uuid.UUID | None) -> MemberBrief | None:
        if member_id is None:
            return None
        return briefs.get(member_id) or MemberBrief(id=member_id, display_name="")

    return DevDocOut(
        id=doc.id,
        work_item_id=doc.work_item_id,
        work_item_title=item.title if item else "",
        author=brief(doc.author_member_id),
        content=doc.content,
        status=doc.status,
        review_note=doc.review_note,
        confirmed_by=brief(doc.confirmed_by),
        confirmed_at=doc.confirmed_at,
        doc_version=doc.doc_version,
        waived=doc.waived,
        latest_review_suggestion_id=await _latest_review_suggestion_id(
            session, doc.work_item_id
        ),
        version=doc.version,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


async def get_dev_doc_for_work_item(
    session: AsyncSession, actor: ProjectMember, item_id: uuid.UUID
) -> DevDocOut:
    """向任务相关成员返回文档及最近一次 AI 初审建议；无文档时返回 `404`。"""
    await get_work_item(session, item_id, project_id=actor.project_id)  # 越权 → 404
    doc = await get_doc(session, item_id)
    if doc is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "该任务还没有开发文档")
    return await _to_out(session, doc)




async def upsert_dev_doc(
    session: AsyncSession, actor: ProjectMember, item_id: uuid.UUID, payload: DevDocUpdateIn
) -> DevDocOut:
    """由主执行人以 `upsert` 方式编辑 `DRAFT` 或 `RETURNED` 文档。"""
    item = await get_work_item(session, item_id, project_id=actor.project_id)  # 越权 → 404
    _require_assignee(actor, item)
    doc = await get_doc(session, item_id, for_update=True)

    if doc is None:
        doc = DevDoc(work_item_id=item.id, author_member_id=actor.id, content=payload.content)
        session.add(doc)
        await session.flush()
        await record_event(
            session,
            actor_id=actor.user_id,
            action="dev_doc.created",
            target_type="dev_doc",
            target_id=doc.id,
            after={"work_item_id": str(item.id), "status": doc.status},
        )
        await session.commit()
        await session.refresh(doc)  # commit 后属性过期，刷新取回避免懒加载
        logger.info("dev doc created: id=%s work_item_id=%s", doc.id, item.id)
        return await _to_out(session, doc)

    if payload.version is None:
        raise ApiException(422, ErrorCodes.VALIDATION_ERROR, "更新已有文档需携带 version")
    _check_version(doc, payload.version)
    if doc.status not in (DevDocStatus.DRAFT.value, DevDocStatus.RETURNED.value):
        raise ApiException(
            409,
            ErrorCodes.DEV_DOC_INVALID_TRANSITION,
            f"文档当前状态 {doc.status} 不可编辑",
            details={"status": doc.status},
        )
    doc.content = payload.content
    doc.version += 1
    await session.flush()
    await record_event(
        session,
        actor_id=actor.user_id,
        action="dev_doc.updated",
        target_type="dev_doc",
        target_id=doc.id,
        after={"status": doc.status},
    )
    await session.commit()
    await session.refresh(doc)
    logger.info("dev doc updated: id=%s work_item_id=%s", doc.id, item.id)
    return await _to_out(session, doc)


async def submit_dev_doc(
    session: AsyncSession, actor: ProjectMember, item_id: uuid.UUID, version: int
) -> DevDocOut:
    """由主执行人提交非空文档，并递增 `doc_version`。

    业务事务 `commit` 后尽力触发 `dev_doc_review` Agent 初审。
    """
    item = await get_work_item(session, item_id, project_id=actor.project_id)  # 越权 → 404
    _require_assignee(actor, item)
    doc = await get_doc(session, item_id, for_update=True)
    if doc is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "请先撰写开发文档再提交审核")
    _check_version(doc, version)
    if not doc.content.strip():
        raise ApiException(422, ErrorCodes.VALIDATION_ERROR, "开发文档内容不能为空")

    before_status = doc.status
    doc.status = transition(doc.status, "submit").value
    doc.author_member_id = actor.id  # 撰写人 = 提交时的主执行人
    doc.review_note = None  # 重新提交时清空上次打回理由
    doc.doc_version += 1
    doc.version += 1
    await session.flush()
    await record_event(
        session,
        actor_id=actor.user_id,
        action="dev_doc.submitted",
        target_type="dev_doc",
        target_id=doc.id,
        before={"status": before_status},
        after={"status": doc.status, "doc_version": doc.doc_version},
    )
    await session.commit()
    await session.refresh(doc)
    logger.info(
        "dev doc submitted: id=%s work_item_id=%s doc_version=%s",
        doc.id,
        item.id,
        doc.doc_version,
    )
    await _dispatch_dev_doc_review(session, item)
    return await _to_out(session, doc)


async def confirm_dev_doc(
    session: AsyncSession, actor: ProjectMember, item_id: uuid.UUID, version: int
) -> DevDocOut:
    """由负责人确认文档，并记录确认人、时间和审计事件。"""
    await get_work_item(session, item_id, project_id=actor.project_id)  # 越权 → 404
    doc = await get_doc(session, item_id, for_update=True)
    if doc is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "该任务还没有开发文档")
    _check_version(doc, version)

    before_status = doc.status
    doc.status = transition(doc.status, "confirm").value
    doc.confirmed_by = actor.id
    doc.confirmed_at = datetime.now(UTC)
    doc.version += 1
    await session.flush()
    await record_event(
        session,
        actor_id=actor.user_id,
        action="dev_doc.confirmed",
        target_type="dev_doc",
        target_id=doc.id,
        before={"status": before_status},
        after={"status": doc.status},
    )
    await session.commit()
    await session.refresh(doc)
    logger.info("dev doc confirmed: id=%s work_item_id=%s", doc.id, item_id)
    return await _to_out(session, doc)


async def return_dev_doc(
    session: AsyncSession,
    actor: ProjectMember,
    item_id: uuid.UUID,
    version: int,
    review_note: str,
) -> DevDocOut:
    """由负责人附理由打回并写审计，允许成员修改后重新提交。"""
    await get_work_item(session, item_id, project_id=actor.project_id)  # 越权 → 404
    doc = await get_doc(session, item_id, for_update=True)
    if doc is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "该任务还没有开发文档")
    _check_version(doc, version)

    before_status = doc.status
    doc.status = transition(doc.status, "return").value
    doc.review_note = review_note
    doc.version += 1
    await session.flush()
    await record_event(
        session,
        actor_id=actor.user_id,
        action="dev_doc.returned",
        target_type="dev_doc",
        target_id=doc.id,
        before={"status": before_status},
        after={"status": doc.status, "review_note": review_note},
    )
    await session.commit()
    await session.refresh(doc)
    logger.info("dev doc returned: id=%s work_item_id=%s", doc.id, item_id)
    return await _to_out(session, doc)


async def waive_dev_doc(
    session: AsyncSession, actor: ProjectMember, item_id: uuid.UUID, version: int | None
) -> DevDocOut:
    """由负责人豁免文档要求；独立标记不改变状态机，并写入审计。"""
    item = await get_work_item(session, item_id, project_id=actor.project_id)  # 越权 → 404
    doc = await get_doc(session, item_id, for_update=True)
    if doc is None:
        # 无文档时创建没有撰写人的占位记录，以持久化豁免状态。
        doc = DevDoc(work_item_id=item.id)
        session.add(doc)
        await session.flush()
    else:
        if version is None:
            raise ApiException(422, ErrorCodes.VALIDATION_ERROR, "豁免已有文档需携带 version")
        _check_version(doc, version)

    doc.waived = True
    doc.waived_by = actor.id
    doc.waived_at = datetime.now(UTC)
    doc.version += 1
    await session.flush()
    await record_event(
        session,
        actor_id=actor.user_id,
        action="dev_doc.waived",
        target_type="dev_doc",
        target_id=doc.id,
        after={"work_item_id": str(item.id), "waived": True},
    )
    await session.commit()
    await session.refresh(doc)
    logger.info("dev doc waived: id=%s work_item_id=%s", doc.id, item.id)
    return await _to_out(session, doc)


async def _dispatch_dev_doc_review(session: AsyncSession, item: WorkItem) -> None:
    """投递事件触发的 `dev_doc_review` Agent 运行；失败时仅记录日志。"""
    redis_client = create_redis_client()
    try:
        run = await request_agent_analysis(
            session,
            redis_client,
            agent_type=DEV_DOC_REVIEW_AGENT_TYPE,
            project_id=item.project_id,
            trigger_source="event",
            work_item_id=item.id,
        )
        logger.info("dev doc review dispatched: run_id=%s work_item_id=%s", run.id, item.id)
    except Exception:  # noqa: BLE001 - Agent 投递失败不能影响已完成的提交
        logger.warning(
            "dev doc review dispatch failed, submit unaffected: work_item_id=%s", item.id
        )
    finally:
        await redis_client.aclose()
