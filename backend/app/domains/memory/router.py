"""项目记忆检索、核心记忆、成员档案和知识库问答接口。

成员只能读取当前项目，全局管理员可进行跨项目只读监督。`HTTP` 检索只接受
`member_qa` 和仅负责人可用的 `leader_query`；`agent_assignment` 仅供内部运行使用。
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiException, ErrorCodes
from app.core.logging import setup_logging
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
    get_member_or_readonly_admin,
    project_id_from_request,
)
from app.domains.project.models import ROLE_LEADER, ProjectMember
from app.infrastructure.database.engine import get_session
from app.infrastructure.models.errors import ModelError

router = APIRouter(tags=["memory"])

logger = setup_logging("backend")


@router.post("/memory/search", response_model=MemorySearchResponse)
async def search_memory_endpoint(
    body: MemorySearchRequest,
    request_id: uuid.UUID = Depends(project_id_from_request),
    auth: tuple[ProjectMember | None, bool] = Depends(get_member_or_readonly_admin),
    session: AsyncSession = Depends(get_session),
) -> MemorySearchResponse:
    member, is_admin = auth

    caller = body.caller or CALLER_MEMBER_QA
    if caller not in (CALLER_MEMBER_QA, CALLER_LEADER_QUERY):
        # 禁止客户端冒充内部 `Agent` 调用以绕过成员鉴权
        raise ApiException(403, ErrorCodes.FORBIDDEN, "不允许的检索调用方标识")
    if caller == CALLER_LEADER_QUERY and (member is None or member.role != ROLE_LEADER):
        # `leader_query` 可读取跨项目档案，因此必须校验负责人角色
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


@router.get("/memory/core-entries", response_model=CoreMemoryEntryListOut)
async def list_core_memory_entries(
    project_id: uuid.UUID = Depends(project_id_from_request),
    _: tuple[ProjectMember | None, bool] = Depends(get_member_or_readonly_admin),
    session: AsyncSession = Depends(get_session),
) -> CoreMemoryEntryListOut:
    """返回核心记忆条目及容量占用；全局管理员仅可只读查看。"""
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
    """由负责人手写并立即生效一条核心记忆；超出容量预算时拒绝。"""
    entry = await create_entry(session, leader, content=body.content)
    return (await entries_to_out(session, [entry]))[0]


@router.post("/memory/core-entries/{entry_id}/deprecate", response_model=CoreMemoryEntryOut)
async def deprecate_core_memory_entry(
    entry_id: uuid.UUID,
    leader: ProjectMember = Depends(get_current_leader),
    session: AsyncSession = Depends(get_session),
) -> CoreMemoryEntryOut:
    """由负责人作废条目并保留追溯记录；跨项目访问按不存在处理。"""
    entry = await deprecate_entry(session, leader, entry_id=entry_id)
    return (await entries_to_out(session, [entry]))[0]


@router.get("/memory/member-stats", response_model=list[MemberStatsOut])
async def list_member_stats(
    project_id: uuid.UUID = Depends(project_id_from_request),
    _: tuple[ProjectMember | None, bool] = Depends(get_member_or_readonly_admin),
    session: AsyncSession = Depends(get_session),
) -> list[MemberStatsOut]:
    """返回严格按项目统计的成员完成数、按时率和负载。

    结果包含停用成员以保留历史口径；全局管理员仅可只读查看。
    """
    stats = await member_completion_stats(session, project_id=project_id)
    return [MemberStatsOut.from_stats(s) for s in stats]


@router.get("/memory/member-profiles/{user_id}", response_model=MemberProfileOut)
async def get_member_profile(
    user_id: uuid.UUID,
    project_id: uuid.UUID = Depends(project_id_from_request),
    _: tuple[ProjectMember | None, bool] = Depends(get_member_or_readonly_admin),
    session: AsyncSession = Depends(get_session),
) -> MemberProfileOut:
    """读取跨项目共享的成员档案。

    项目成员和被评价者本人均可读取，全局管理员仅可只读查看；无档案时返回 404。
    """
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
    """由负责人创建或更新成员档案，提交后立即生效。"""
    profile = await upsert_profile(session, leader, user_id=user_id, content=body.content)
    return await profile_to_out(session, profile, project_id=leader.project_id)


@router.post("/memory/qa", response_model=MemoryQaResponse)
async def ask_knowledge_base(
    body: MemoryQaRequest,
    member: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> MemoryQaResponse:
    """根据项目记忆回答成员问题，依据不足时拒答并返回线索。

    问答是生成服务，仅项目成员可用；全局管理员应使用只读列表或检索接口。
    模型不可用时返回 503，查询和问答不写入审计事件。
    """
    try:
        result = await answer_question(
            session, member=member, project_id=member.project_id, query=body.question
        )
    except ModelError as exc:
        raise ApiException(
            503, ErrorCodes.INTERNAL_ERROR, "模型服务暂不可用，请稍后重试"
        ) from exc

    # 历史写入失败不影响回答；为避免日志泄露问题、答案和依据，只记录异常类型
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
    """分页返回本人问答历史；负责人和全局管理员也不能查看他人记录。"""
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
