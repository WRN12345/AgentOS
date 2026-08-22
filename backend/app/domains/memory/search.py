"""记忆检索权限层（设计文档第 12 节）：Agent 与问答页面共用的唯一检索入口。

规则：
- 项目在职成员可检索本项目；跨项目访问返回 404（不暴露存在性，多项目规约）；
- 全局 admin 只读可查任意项目（监督审计），不依赖项目成员身份；
- 调用方标识 caller（leader_query / agent_assignment / member_qa）贯穿到检索层，
  成员档案的跨项目放行规则（16.12，M3.9）依此判定——普通成员问答不命中
  他项目无关人员档案。
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiException, ErrorCodes
from app.domains.memory.retriever import MemoryRetriever, RetrievalResult
from app.domains.project.models import ProjectMember

#: 检索调用方标识（16.12）：档案跨项目放行依此判定
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
    """带权限校验的检索：项目成员限本项目，全局 admin 只读任意项目。

    - member 为 None 且非 admin → 404；member 与 project_id 不匹配/已停用 → 404；
    - caller 必须是 CALLER_TYPES 之一（档案放行规则的判定依据，M3.9）。
    """
    if caller not in CALLER_TYPES:
        raise ValueError(f"未知检索调用方: {caller}")

    if member is not None:
        if not member.is_active or member.project_id != project_id:
            raise ApiException(404, ErrorCodes.NOT_FOUND, "项目不存在")
    elif not is_admin:
        raise ApiException(404, ErrorCodes.NOT_FOUND, "项目不存在")

    retriever = MemoryRetriever(session)
    return await retriever.search(
        query,
        project_id=project_id,
        source_types=source_types,
        limit=limit,
        max_distance=max_distance,
    )
