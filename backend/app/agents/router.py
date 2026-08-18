"""Agent 人工触发接口（12.5 节，T5.4/T5.5）。

- POST /work-items/{id}/agent-analysis  项目负责人或该工作项相关成员：
  创建 agent_runs(pending) 并投递 agent.run 队列任务，202 返回运行信息。
- POST /agent-analysis  仅项目负责人（T5.5）：项目级 Agent 触发入口，
  work_item_id 可空（风险扫描、项目摘要等无单一工作项的分析）。
- POST /agent-runs/{id}/retry  负责人或关联工作项相关成员（T5.6）：
  人工重新触发失败的运行，仅 failed 可重试（其余 409），202 重新投递。

Agent 只产出建议（agent_suggestions），不具备写业务工具（10.3 节，T5.3
护栏）；正式工作项创建仍走 POST /work-items。T5.7 落地建议查询与反馈：
- GET  /agent-suggestions                登录成员可读：按类型/反馈状态/关联工作项过滤
- POST /agent-suggestions/{id}/feedback  仅负责人：采纳/忽略，重复反馈 409
- GET  /agent-runs[/{id}]                登录成员可读：运行记录（失败重触发入口）
"""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.graphs.base import AGENT_ROUTES
from app.agents.models import AgentRun, AgentSuggestion
from app.agents.schemas.analysis import AgentAnalysisIn, AgentRunOut, ProjectAgentAnalysisIn
from app.agents.schemas.suggestions import AgentSuggestionFeedbackIn, AgentSuggestionOut
from app.agents.service import (
    list_suggestions,
    raise_if_suggestion_reviewed,
    request_agent_analysis,
    retry_agent_run,
    submit_suggestion_feedback,
)
from app.core.errors import ApiException, ErrorCodes
from app.core.logging import setup_logging
from app.core.request_context import get_request_id
from app.domains.files.service import is_work_item_related
from app.domains.project.dependencies import get_current_leader, get_current_member
from app.domains.project.models import ROLE_LEADER, ProjectMember
from app.domains.work_items.service import get_work_item
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import get_session

logger = setup_logging("backend")

router = APIRouter(tags=["agents"])


def _run_out(run: AgentRun, *, with_details: bool = False) -> AgentRunOut:
    """agent_runs → 出参；列表/详情携带错误与耗时（触发响应保持精简）。"""
    return AgentRunOut(
        id=run.id,
        agent_type=run.agent_type,
        status=run.status,
        model=run.model,
        trigger_source=run.trigger_source,
        work_item_id=run.work_item_id,
        request_id=run.request_id,
        created_at=run.created_at,
        error=run.error if with_details else None,
        duration_ms=run.duration_ms if with_details else None,
        retry_count=run.retry_count if with_details else 0,
    )


async def _get_run_in_project(
    session: AsyncSession, run_id: uuid.UUID, project_id: uuid.UUID
) -> AgentRun | None:
    """按项目取运行记录（ticket 05）：跨项目或不存在都返回 None，调用方统一 404，
    不泄漏其他项目资源的存在性。"""
    run = await session.get(AgentRun, run_id)
    if run is None or run.project_id != project_id:
        return None
    return run


@router.post(
    "/work-items/{item_id}/agent-analysis",
    response_model=AgentRunOut,
    status_code=202,
)
async def request_agent_analysis_endpoint(
    item_id: uuid.UUID,
    payload: AgentAnalysisIn,
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> AgentRunOut:
    """人工触发一次 Agent 分析（12.5 节）。

    权限：项目负责人或该工作项相关成员（主执行人/协作者/协作请求任一方，
    与文件下载同一套"相关"判定，16 节）；工作项不存在 404，无关成员 403，
    未注册的 agent_type 400。
    """
    if payload.agent_type not in AGENT_ROUTES:
        raise ApiException(
            400,
            ErrorCodes.VALIDATION_ERROR,
            "未注册的 Agent 类型",
            details={"agent_type": payload.agent_type, "registered": sorted(AGENT_ROUTES)},
        )
    item = await get_work_item(session, item_id, project_id=actor.project_id)  # 墙外同样 404
    if actor.role != ROLE_LEADER and not await is_work_item_related(session, item.id, actor.id):
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅项目负责人或工作项相关成员可触发 Agent 分析")

    redis_client = create_redis_client()
    try:
        run = await request_agent_analysis(
            session,
            redis_client,
            agent_type=payload.agent_type,
            project_id=actor.project_id,
            trigger_source="manual",
            work_item_id=item.id,
            prompt=payload.prompt,
            request_id=get_request_id() or None,
        )
    finally:
        await redis_client.aclose()
    logger.info(
        "agent analysis requested: run_id=%s agent_type=%s work_item_id=%s actor=%s",
        run.id,
        run.agent_type,
        item.id,
        actor.id,
    )
    return AgentRunOut(
        id=run.id,
        agent_type=run.agent_type,
        status=run.status,
        model=run.model,
        trigger_source=run.trigger_source,
        work_item_id=run.work_item_id,
        request_id=run.request_id,
        created_at=run.created_at,
    )


@router.post(
    "/agent-analysis",
    response_model=AgentRunOut,
    status_code=202,
)
async def request_project_agent_analysis_endpoint(
    payload: ProjectAgentAnalysisIn,
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> AgentRunOut:
    """项目级 Agent 分析触发入口（T5.5）。

    权限：仅项目负责人（项目级数据面向负责人汇总，16 节）；
    work_item_id 可空，给出时校验工作项存在（404）；
    未注册的 agent_type 400。
    """
    if payload.agent_type not in AGENT_ROUTES:
        raise ApiException(
            400,
            ErrorCodes.VALIDATION_ERROR,
            "未注册的 Agent 类型",
            details={"agent_type": payload.agent_type, "registered": sorted(AGENT_ROUTES)},
        )
    if actor.role != ROLE_LEADER:
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅项目负责人可触发项目级 Agent 分析")
    if payload.work_item_id is not None:
        await get_work_item(
            session, payload.work_item_id, project_id=actor.project_id
        )  # 不存在或跨项目 → 404

    redis_client = create_redis_client()
    try:
        run = await request_agent_analysis(
            session,
            redis_client,
            agent_type=payload.agent_type,
            project_id=actor.project_id,
            trigger_source="manual",
            work_item_id=payload.work_item_id,
            prompt=payload.prompt,
            request_id=get_request_id() or None,
        )
    finally:
        await redis_client.aclose()
    logger.info(
        "project agent analysis requested: run_id=%s agent_type=%s work_item_id=%s actor=%s",
        run.id,
        run.agent_type,
        payload.work_item_id,
        actor.id,
    )
    return AgentRunOut(
        id=run.id,
        agent_type=run.agent_type,
        status=run.status,
        model=run.model,
        trigger_source=run.trigger_source,
        work_item_id=run.work_item_id,
        request_id=run.request_id,
        created_at=run.created_at,
    )


@router.post(
    "/agent-runs/{run_id}/retry",
    response_model=AgentRunOut,
    status_code=202,
)
async def retry_agent_run_endpoint(
    run_id: uuid.UUID,
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> AgentRunOut:
    """人工重新触发失败的 Agent 运行（17.3 节，T5.6）。

    权限：项目负责人，或 run 关联工作项的相关成员（与触发接口同一套
    "相关"判定）；项目级 run（无 work_item_id）仅负责人可重试。
    仅 failed 状态可重试（其余 409）；重置为 pending 按原输入重新投递，
    202 返回运行信息。
    """
    run = await _get_run_in_project(session, run_id, actor.project_id)
    if run is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "Agent 运行不存在")
    if actor.role != ROLE_LEADER:
        related = run.work_item_id is not None and await is_work_item_related(
            session, run.work_item_id, actor.id
        )
        if not related:
            raise ApiException(403, ErrorCodes.FORBIDDEN, "仅项目负责人或工作项相关成员可重新触发 Agent 运行")
    if run.status != "failed":
        raise ApiException(
            409,
            ErrorCodes.AGENT_RUN_NOT_FAILED,
            "仅失败的 Agent 运行可人工重新触发",
            details={"run_id": str(run.id), "status": run.status},
        )

    redis_client = create_redis_client()
    try:
        run = await retry_agent_run(session, redis_client, run)
    finally:
        await redis_client.aclose()
    logger.info(
        "agent run retry requested: run_id=%s agent_type=%s actor=%s",
        run.id,
        run.agent_type,
        actor.id,
    )
    return AgentRunOut(
        id=run.id,
        agent_type=run.agent_type,
        status=run.status,
        model=run.model,
        trigger_source=run.trigger_source,
        work_item_id=run.work_item_id,
        request_id=run.request_id,
        created_at=run.created_at,
    )


@router.get("/agent-suggestions", response_model=list[AgentSuggestionOut])
async def list_agent_suggestions_endpoint(
    suggestion_type: str | None = Query(default=None),
    review_status: str | None = Query(default=None),
    work_item_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> list[AgentSuggestionOut]:
    """建议查询（12.5 节，T5.7）。

    权限：登录项目成员均可读——建议不含敏感信息（内容与理由面向全员透明），
    反馈操作限负责人（见 feedback 端点）；13.1 节建议中心虽以负责人为主，
    成员查看建议与团队透明看板语义一致。
    项目隔离（ticket 05）：只返回当前项目（actor.project_id）的建议，
    经 run 推导归属，agent_suggestions 不冗余 project_id。
    分页与现有列表接口一致（limit/offset，返回当前页数组）。
    """
    rows = await list_suggestions(
        session,
        project_id=actor.project_id,
        suggestion_type=suggestion_type,
        review_status=review_status,
        work_item_id=work_item_id,
        limit=limit,
        offset=offset,
    )
    return [
        AgentSuggestionOut(
            id=s.id,
            run_id=s.run_id,
            suggestion_type=s.suggestion_type,
            content=s.content,
            confidence=s.confidence,
            risks=s.risks,
            fact_refs=s.fact_refs,
            review_status=s.review_status,
            reviewed_by=s.reviewed_by,
            reviewed_at=s.reviewed_at,
            prompt_version=s.prompt_version,
            work_item_id=run.work_item_id,
            model=run.model,
            created_at=s.created_at,
        )
        for s, run in rows
    ]


@router.post("/agent-suggestions/{suggestion_id}/feedback", response_model=AgentSuggestionOut)
async def submit_suggestion_feedback_endpoint(
    suggestion_id: uuid.UUID,
    payload: AgentSuggestionFeedbackIn,
    actor: ProjectMember = Depends(get_current_leader),
    session: AsyncSession = Depends(get_session),
) -> AgentSuggestionOut:
    """人工采纳/忽略反馈（12.5 节，T5.7）。

    权限：仅项目负责人（13.1 节建议中心为负责人页面，反馈决定建议命运）。
    状态迁移：仅 pending 可反馈，重复反馈 409 AGENT_SUGGESTION_ALREADY_REVIEWED；
    写 agent.suggestion_feedback 审计事件。反馈只落在 agent_suggestions
    自身，不产生任何业务写入（采纳后的业务动作由前端走正式命令接口）。
    """
    suggestion = await session.get(AgentSuggestion, suggestion_id)
    if suggestion is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "Agent 建议不存在")
    # 项目归属校验（ticket 05）：建议经 run 推导归属，跨项目视为不存在 → 404
    run = await session.get(AgentRun, suggestion.run_id)
    if run is None or run.project_id != actor.project_id:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "Agent 建议不存在")
    raise_if_suggestion_reviewed(suggestion)
    suggestion = await submit_suggestion_feedback(
        session, suggestion, action=payload.action, actor_id=actor.id
    )
    run = await session.get(AgentRun, suggestion.run_id)
    return AgentSuggestionOut(
        id=suggestion.id,
        run_id=suggestion.run_id,
        suggestion_type=suggestion.suggestion_type,
        content=suggestion.content,
        confidence=suggestion.confidence,
        risks=suggestion.risks,
        fact_refs=suggestion.fact_refs,
        review_status=suggestion.review_status,
        reviewed_by=suggestion.reviewed_by,
        reviewed_at=suggestion.reviewed_at,
        prompt_version=suggestion.prompt_version,
        work_item_id=run.work_item_id if run else None,
        model=run.model if run else None,
        created_at=suggestion.created_at,
    )


@router.get("/agent-runs", response_model=list[AgentRunOut])
async def list_agent_runs_endpoint(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> list[AgentRunOut]:
    """运行记录列表（T5.7）：建议中心展示运行状态，failed 供人工重新触发。

    权限：登录成员可读（与建议查询同策略：无敏感信息，反馈/触发仍限权）。
    项目隔离（ticket 05）：只返回当前项目（actor.project_id）的运行记录。
    """
    stmt = (
        select(AgentRun)
        .where(AgentRun.project_id == actor.project_id)
        .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if status:
        stmt = stmt.where(AgentRun.status == status)
    runs = list((await session.execute(stmt)).scalars().all())
    return [_run_out(run, with_details=True) for run in runs]


@router.get("/agent-runs/{run_id}", response_model=AgentRunOut)
async def get_agent_run_endpoint(
    run_id: uuid.UUID,
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> AgentRunOut:
    """单个运行记录（创建工作项引导等场景轮询运行状态用）。

    项目隔离（ticket 05）：跨项目运行视为不存在 → 404。
    """
    run = await _get_run_in_project(session, run_id, actor.project_id)
    if run is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "Agent 运行不存在")
    return _run_out(run, with_details=True)
