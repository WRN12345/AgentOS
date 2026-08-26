"""记忆检索接口（设计文档第 11、12 节）。

- POST /memory/search：项目成员检索本项目记忆；全局 admin 只读可查任意项目；
- 与 Agent 工具共用 search_memory 权限路径——检索层强制项目隔离（第 12 节）；
- caller 标识：HTTP 路径仅允许 member_qa（默认）与 leader_query（需负责人角色），
  agent_assignment 仅供 Agent 内部调用（16.12，M3.9 放行规则的判定依据）。
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiException, ErrorCodes
from app.core.logging import setup_logging
from app.domains.identity.dependencies import get_current_user
from app.domains.identity.models import User
from app.domains.memory.core_memory import (
    budget_usage,
    create_entry,
    deprecate_entry,
    entries_to_out,
    list_entries,
)
from app.domains.memory.member_profiles import get_profile, profile_to_out, upsert_profile
from app.domains.memory.member_stats import member_completion_stats
from app.domains.memory.qa import answer_question, list_qa_history, save_qa_history
from app.domains.memory.schemas import (
    CoreMemoryEntryCreateIn,
    CoreMemoryEntryListOut,
    CoreMemoryEntryOut,
    MemberProfileOut,
    MemberProfileUpsertIn,
    MemberStatsOut,
    MemoryQaRequest,
    MemoryQaResponse,
    MemorySearchRequest,
    MemorySearchResponse,
    QaHistoryOut,
    QaSourceOut,
)
from app.domains.memory.search import (
    CALLER_LEADER_QUERY,
    CALLER_MEMBER_QA,
    search_memory,
)
from app.domains.project.dependencies import (
    get_current_leader,
    get_current_member,
    project_id_from_request,
)
from app.domains.project.models import ROLE_LEADER, ProjectMember
from app.domains.project.service import get_member_by_user
from app.infrastructure.database.engine import get_session
from app.infrastructure.models.errors import ModelError

router = APIRouter(tags=["memory"])

logger = setup_logging("backend")


@router.post("/memory/search", response_model=MemorySearchResponse)
async def search_memory_endpoint(
    body: MemorySearchRequest,
    request_id: uuid.UUID = Depends(project_id_from_request),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MemorySearchResponse:
    member = await get_member_by_user(session, request_id, current_user.id)
    is_admin = bool(current_user.is_admin)
    if member is None or not member.is_active:
        if not is_admin:
            raise ApiException(403, ErrorCodes.NOT_PROJECT_MEMBER, "当前账号不是该项目成员或已被禁用")
        member = None  # 全局 admin：只读查看（第 12 节）

    caller = body.caller or CALLER_MEMBER_QA
    if caller not in (CALLER_MEMBER_QA, CALLER_LEADER_QUERY):
        # agent_assignment 仅供 Agent 内部调用（16.12），HTTP 路径不开放
        raise ApiException(403, ErrorCodes.FORBIDDEN, "不允许的检索调用方标识")
    if caller == CALLER_LEADER_QUERY and (member is None or member.role != ROLE_LEADER):
        # leader_query 会命中档案跨项目内容（M3.9），HTTP 路径须确为负责人
        raise ApiException(403, ErrorCodes.FORBIDDEN, "仅项目负责人可按分配场景检索")

    results = await search_memory(
        session,
        member=member,
        is_admin=is_admin,
        project_id=request_id,
        query=body.query,
        caller=caller,
        source_types=body.source_types,
        limit=body.limit,
    )
    return MemorySearchResponse.from_results(results)


# ---------- 核心记忆条目（设计文档第 8 节，M4.3） ----------


@router.get("/memory/core-entries", response_model=CoreMemoryEntryListOut)
async def list_core_memory_entries(
    project_id: uuid.UUID = Depends(project_id_from_request),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CoreMemoryEntryListOut:
    """核心记忆条目列表 + 容量占用：项目成员可读；全局 admin 只读查看（第 12 节）。"""
    member = await get_member_by_user(session, project_id, current_user.id)
    if member is None or not member.is_active:
        if not current_user.is_admin:
            raise ApiException(403, ErrorCodes.NOT_PROJECT_MEMBER, "当前账号不是该项目成员或已被禁用")

    entries = await list_entries(session, project_id=project_id)
    used, budget = await budget_usage(session, project_id=project_id)
    return CoreMemoryEntryListOut(
        entries=await entries_to_out(session, entries),
        used_chars=used,
        budget_chars=budget,
    )


@router.post("/memory/core-entries", response_model=CoreMemoryEntryOut, status_code=201)
async def create_core_memory_entry(
    body: CoreMemoryEntryCreateIn,
    leader: ProjectMember = Depends(get_current_leader),
    session: AsyncSession = Depends(get_session),
) -> CoreMemoryEntryOut:
    """负责人手写条目（种子记忆，16.11），立即生效；超容量预算 400 拒绝。"""
    entry = await create_entry(session, leader, content=body.content)
    return (await entries_to_out(session, [entry]))[0]


@router.post("/memory/core-entries/{entry_id}/deprecate", response_model=CoreMemoryEntryOut)
async def deprecate_core_memory_entry(
    entry_id: uuid.UUID,
    leader: ProjectMember = Depends(get_current_leader),
    session: AsyncSession = Depends(get_session),
) -> CoreMemoryEntryOut:
    """负责人作废条目：保留供追溯；跨项目按 404（多项目规约）。"""
    entry = await deprecate_entry(session, leader, entry_id=entry_id)
    return (await entries_to_out(session, [entry]))[0]


# ---------- 团队记忆：成员统计（设计文档第 7 节①，M3.3） ----------


@router.get("/memory/member-stats", response_model=list[MemberStatsOut])
async def list_member_stats(
    project_id: uuid.UUID = Depends(project_id_from_request),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[MemberStatsOut]:
    """成员完成数/按时率/负载统计：项目成员可查（分配页面与 Agent 工具共用）；
    全局 admin 只读查看（第 12 节）。统计严格项目内口径（含停用成员，16.7）。
    """
    member = await get_member_by_user(session, project_id, current_user.id)
    if member is None or not member.is_active:
        if not current_user.is_admin:
            raise ApiException(403, ErrorCodes.NOT_PROJECT_MEMBER, "当前账号不是该项目成员或已被禁用")

    stats = await member_completion_stats(session, project_id=project_id)
    return [MemberStatsOut.from_stats(s) for s in stats]


# ---------- 团队记忆：成员文字档案（设计文档第 7 节②，M3.5） ----------


@router.get("/memory/member-profiles/{user_id}", response_model=MemberProfileOut)
async def get_member_profile(
    user_id: uuid.UUID,
    project_id: uuid.UUID = Depends(project_id_from_request),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MemberProfileOut:
    """读取成员档案：项目内全员可读（含被评价者本人，16.1），档案随人走跨项目可读；
    全局 admin 只读（第 12 节）。无档案 404（新成员尚无档案属正常）。
    """
    member = await get_member_by_user(session, project_id, current_user.id)
    if member is None or not member.is_active:
        if not current_user.is_admin:
            raise ApiException(403, ErrorCodes.NOT_PROJECT_MEMBER, "当前账号不是该项目成员或已被禁用")

    profile = await get_profile(session, user_id)
    if profile is None:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "该成员暂无档案")
    return await profile_to_out(session, profile, project_id=project_id)


@router.put("/memory/member-profiles/{user_id}", response_model=MemberProfileOut)
async def upsert_member_profile(
    user_id: uuid.UUID,
    body: MemberProfileUpsertIn,
    leader: ProjectMember = Depends(get_current_leader),
    session: AsyncSession = Depends(get_session),
) -> MemberProfileOut:
    """创建/更新成员档案：仅负责人（15.6），写完直接生效（第 7 节）。"""
    profile = await upsert_profile(session, leader, user_id=user_id, content=body.content)
    return await profile_to_out(session, profile, project_id=leader.project_id)


# ---------- 知识库问答（设计文档第 11 节②，M7.3） ----------


@router.post("/memory/qa", response_model=MemoryQaResponse)
async def ask_knowledge_base(
    body: MemoryQaRequest,
    member: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> MemoryQaResponse:
    """一问一答：项目成员提问，命中则生成回答并附依据，低于阈值拒答并给线索。

    仅项目成员可用：问答是生成服务而非内容查看，全局 admin 的只读查看
    走列表/检索接口（第 12 节），不经此路径。模型不可用时返回 503
    （明确失败，不编造）；查询与问答不审计（16.10）。
    """
    try:
        result = await answer_question(
            session, member=member, project_id=member.project_id, query=body.question
        )
    except ModelError as exc:
        raise ApiException(
            503, ErrorCodes.INTERNAL_ERROR, "模型服务暂不可用，请稍后重试"
        ) from exc

    # 问答历史按人落库（2026-08-24 修订，仅本人可见）；best-effort：
    # 历史写入失败不影响问答本身。INSERT 参数含问题/答案/依据片段，
    # 异常文本会带出这些私人内容，因此只记异常类型，不记堆栈与参数
    try:
        await save_qa_history(session, member=member, query=body.question, result=result)
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        logger.warning(
            "qa history save failed, answer unaffected: %s", type(exc).__name__
        )

    return MemoryQaResponse(
        status=result.status,
        answer=result.answer,
        sources=[
            QaSourceOut(
                source_type=s.source_type,
                source_id=s.source_id,
                title=s.title,
                snippet=s.snippet,
                history_kind=s.history_kind,
            )
            for s in result.sources
        ],
        clues=[
            QaSourceOut(
                source_type=c.source_type,
                source_id=c.source_id,
                title=c.title,
                snippet=c.snippet,
                history_kind=c.history_kind,
            )
            for c in result.clues
        ],
    )


@router.get("/memory/qa/history", response_model=list[QaHistoryOut])
async def list_my_qa_history(
    limit: int = 50,
    offset: int = 0,
    member: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> list[QaHistoryOut]:
    """本人问答历史（时间倒序，分页）。仅本人可见——负责人/admin 无成员身份
    或查他人历史均无此路径（2026-08-24 决策修订）。"""
    records = await list_qa_history(session, member=member, limit=limit, offset=offset)
    return [
        QaHistoryOut(
            id=r.id,
            question=r.question,
            status=r.status,
            answer=r.answer,
            sources=[
                QaSourceOut(
                    source_type=s["source_type"],
                    source_id=uuid.UUID(s["source_id"]),
                    title=s["title"],
                    snippet=s["snippet"],
                    history_kind=s.get("history_kind"),
                )
                for s in r.sources
            ],
            created_at=r.created_at,
        )
        for r in records
    ]
