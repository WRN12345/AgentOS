"""`Agent` 与问答接口共用的记忆检索权限层。

有效成员只能检索本项目，跨项目访问按不存在处理；全局管理员可以只读检索任意项目。
`caller` 决定是否允许跨项目读取成员档案，普通成员问答不会命中档案。
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiException, ErrorCodes
from app.domains.memory.retriever import MemoryRetriever, RetrievalResult
from app.domains.project.models import ProjectMember

#: 调用方标识是成员档案跨项目放行的安全边界
CALLER_LEADER_QUERY = "leader_query"
CALLER_AGENT_ASSIGNMENT = "agent_assignment"
CALLER_MEMBER_QA = "member_qa"
CALLER_TYPES = frozenset({CALLER_LEADER_QUERY, CALLER_AGENT_ASSIGNMENT, CALLER_MEMBER_QA})


async def search_memory(
    session: AsyncSession,
    *,
    member: ProjectMember | None,
    is_admin: bool,
    project_id: uuid.UUID,
    query: str,
    caller: str,
    source_types: list[str] | None = None,
    limit: int | None = None,
    max_distance: float | None = None,
) -> list[RetrievalResult]:
    """执行带权限校验的记忆检索。

    `member` 缺失、停用或项目不匹配时按项目不存在处理，全局管理员除外。
    `agent_assignment` 仅用于内部运行，其项目上下文必须由服务端运行记录建立，
    `HTTP` 路径不得接受该调用方标识。
    """
    if caller not in CALLER_TYPES:
        raise ValueError(f"未知检索调用方: {caller}")

    if caller == CALLER_AGENT_ASSIGNMENT:
        # 内部 `Agent` 调用信任服务端运行记录中的项目归属
        pass
    elif member is not None:
        if not member.is_active or member.project_id != project_id:
            raise ApiException(404, ErrorCodes.NOT_FOUND, "项目不存在")
    elif not is_admin:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "项目不存在")

    # 只有负责人查询和内部分配可以读取不绑定项目的成员档案
    allow_profiles = caller in (CALLER_LEADER_QUERY, CALLER_AGENT_ASSIGNMENT)
    if not allow_profiles and source_types and "profile" in source_types:
        source_types = [t for t in source_types if t != "profile"]
        if not source_types:
            return []

    retriever = MemoryRetriever(session)
    return await retriever.search(
        query,
        project_id=project_id,
        source_types=source_types,
        limit=limit,
        max_distance=max_distance,
        include_cross_project_profiles=allow_profiles,
    )
