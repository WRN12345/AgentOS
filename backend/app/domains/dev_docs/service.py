"""开发文档应用服务与权限策略（设计文档 2026-07-30 §3/§4.2，16 节）。

权限规则（每个用例显式校验）：
- 撰写/编辑（PUT）、提交审核（submit）：仅工作项当前主执行人；
- 确认（confirm）/ 打回（return）/ 豁免（waive）：仅项目负责人（路由层
  get_current_leader）；
- 查询：任何项目成员（原则 6 透明）。

核心约束：
- 每个工作项一份文档（work_item_id 唯一）；DRAFT/RETURNED 可编辑，
  SUBMITTED/CONFIRMED 只读（状态机裁决，与转派/DDL 同一模式）；
- submit 要求内容非空，doc_version +1，并在业务事务 commit 后触发
  dev_doc_review Agent 初审（trigger_source="event"，尽力而为，失败不影响
  已完成的提交，17.3 节）；
- 豁免（waive）为独立标记：负责人对纯管理类任务免除文档要求，写审计；
- 每次状态/内容变更与同事务写审计事件（原则 5）。
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


# ---------- 查询与序列化 ----------


async def get_doc(
    session: AsyncSession, work_item_id: uuid.UUID, *, for_update: bool = False
) -> DevDoc | None:
    """按工作项取开发文档（无文档返回 None；写路径 for_update 持行锁，17.2 节）。"""
    stmt = select(DevDoc).where(DevDoc.work_item_id == work_item_id)
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


def _check_version(doc: DevDoc, version: int) -> None:
    """乐观锁校验：版本不匹配返回 409（17.2 节）。"""
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
    """最近一次 dev_doc_review 初审建议 ID（LLM 不可用时为 None，降级不阻塞）。"""
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
    """任务相关成员可读（含最近一次 AI 初审建议关联）；无文档 404。"""
    await get_work_item(session, item_id, project_id=actor.project_id)  # 越权 → 404
    doc = await get_doc(session, item_id)
    if doc is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "该任务还没有开发文档")
    return await _to_out(session, doc)


# ---------- 写命令 ----------


async def upsert_dev_doc(
    session: AsyncSession, actor: ProjectMember, item_id: uuid.UUID, payload: DevDocUpdateIn
) -> DevDocOut:
    """撰写/编辑草稿（upsert）：仅主执行人；DRAFT/RETURNED 可编辑（乐观锁）。"""
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
    """提交审核：仅主执行人；内容非空；DRAFT/RETURNED → SUBMITTED，doc_version +1。

    业务事务 commit 后触发 dev_doc_review Agent 初审（event 触发，尽力而为）。
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
    """确认通过：仅负责人；SUBMITTED → CONFIRMED（记录确认人/时间，写审计）。"""
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
    """打回：仅负责人；SUBMITTED → RETURNED，附理由（写审计），成员修改后可重交。"""
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
    """豁免文档要求：仅负责人；独立标记不改变状态机（写审计）。"""
    item = await get_work_item(session, item_id, project_id=actor.project_id)  # 越权 → 404
    doc = await get_doc(session, item_id, for_update=True)
    if doc is None:
        # 无文档任务直接豁免：创建占位行（尚无撰写人）
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
    """投递 dev_doc_review 的 agent.run（trigger_source="event"），失败只记日志。"""
    redis_client = create_redis_client()
    try:
        run = await request_agent_analysis(
            session,
            redis_client,
            agent_type=DEV_DOC_REVIEW_AGENT_TYPE,
            trigger_source="event",
            work_item_id=item.id,
        )
        logger.info("dev doc review dispatched: run_id=%s work_item_id=%s", run.id, item.id)
    except Exception:  # noqa: BLE001 - Agent 投递失败不拖垮已完成的提交（17.3 节）
        logger.warning(
            "dev doc review dispatch failed, submit unaffected: work_item_id=%s", item.id
        )
    finally:
        await redis_client.aclose()
