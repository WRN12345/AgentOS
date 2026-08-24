"""知识库问答（设计文档第 11 节②，M7.1）：检索与阈值拒答。

- 一问一答：问题转向量 → M2.9 带权限检索（调用方标识 member_qa——
  项目内全类型，不命中成员档案，16.12）；
- 拒答策略（16.13）：所有命中均低于相似度阈值时**不生成回答**，
  返回最接近的几条线索供人工判断——一次被抓住编答案，知识库的信任就归零，
  宁拒答不编造；
- 阈值沿用 settings.memory_search_max_distance（M2.8 已参数化，可配置）。
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.memory.retriever import RetrievalResult
from app.domains.memory.search import CALLER_MEMBER_QA, search_memory
from app.domains.project.models import ProjectMember

#: 拒答时返回的"最接近线索"条数（16.13）
QA_CLUE_LIMIT = 3


@dataclass(frozen=True)
class QaRetrieval:
    """问答检索结果：可作答时的依据命中，或拒答时的最接近线索。"""

    answerable: bool
    hits: list[RetrievalResult] = field(default_factory=list)
    clues: list[RetrievalResult] = field(default_factory=list)


async def retrieve_for_qa(
    session: AsyncSession,
    *,
    member: ProjectMember,
    project_id: uuid.UUID,
    query: str,
) -> QaRetrieval:
    """问答检索：阈值内命中可作答；否则拒答并给最接近的线索。

    embedding 不可用时 ModelError 冒泡给路由层（检索失败不生成回答，16.13 的
    精神一致：宁可明确失败也不编造）。
    """
    hits = await search_memory(
        session,
        member=member,
        is_admin=False,
        project_id=project_id,
        query=query,
        caller=CALLER_MEMBER_QA,
    )
    if hits:
        return QaRetrieval(answerable=True, hits=hits)

    # 阈值内无命中 → 拒答：取最接近的几条线索（放宽阈值，仅供人工判断）
    clues = await search_memory(
        session,
        member=member,
        is_admin=False,
        project_id=project_id,
        query=query,
        caller=CALLER_MEMBER_QA,
        limit=QA_CLUE_LIMIT,
        max_distance=2.0,  # 余弦距离理论上限，等价于不按相似度过滤
    )
    return QaRetrieval(answerable=False, clues=clues)
