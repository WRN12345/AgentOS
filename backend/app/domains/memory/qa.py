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

from app.domains.files.models import StoredFile
from app.domains.memory.retriever import RetrievalResult
from app.domains.memory.search import CALLER_MEMBER_QA, search_memory
from app.domains.project.models import ProjectMember
from app.domains.work_items.models import WorkItem
from app.infrastructure.models.provider import get_model_provider

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


# ---------- 答案生成与依据列表（M7.2，设计文档第 11 节②） ----------

_QA_SYSTEM = (
    "你是项目知识库问答助手。只根据给定的资料片段回答问题，不得使用片段之外"
    "的知识，也不得猜测；片段不足以回答时，直接说『根据现有资料无法回答』。"
    "回答末尾用 [1] [2] 标注每个结论出自哪一段资料（编号与输入一致）。"
)


@dataclass(frozen=True)
class QaSource:
    """一条依据/线索：来源定位 + 片段内容（点击可查看原文，第 11 节）。"""

    source_type: str
    source_id: uuid.UUID
    title: str  # 文件名 / 工作项标题 / 核心记忆条目等展示定位
    snippet: str


@dataclass(frozen=True)
class QaAnswer:
    """一问一答结果：answered 附依据列表；refused 附最接近的线索（16.13）。"""

    status: str  # "answered" | "refused"
    answer: str | None
    sources: list[QaSource] = field(default_factory=list)
    clues: list[QaSource] = field(default_factory=list)


async def _resolve_source_title(session: AsyncSession, r: RetrievalResult) -> str:
    """依据定位展示名：文档→文件名；历史→工作项标题或"需求拆解记录"；其余→类型名。"""
    if r.source_type == "document":
        stored = await session.get(StoredFile, r.source_id)
        return stored.original_filename if stored else "（文档已不存在）"
    if r.source_type == "history":
        item = await session.get(WorkItem, r.source_id)
        if item is not None:
            return f"工作项：{item.title}"
        return "需求拆解/分配记录"
    if r.source_type == "core_memory":
        return "核心记忆条目"
    return r.source_type


async def _to_sources(
    session: AsyncSession, results: list[RetrievalResult]
) -> list[QaSource]:
    return [
        QaSource(
            source_type=r.source_type,
            source_id=r.source_id,
            title=await _resolve_source_title(session, r),
            snippet=r.content,
        )
        for r in results
    ]


async def answer_question(
    session: AsyncSession,
    *,
    member: ProjectMember,
    project_id: uuid.UUID,
    query: str,
) -> QaAnswer:
    """一问一答：有依据时生成回答并附依据列表；无依据拒答并给线索。

    - 提示词要求答案必须基于所给片段（可验证是知识库被信任的基础，第 11 节）；
    - 查询与问答不审计（16.10：避免"提问被记录"抑制使用）；
    - 模型/embedding 不可用时 ModelError 冒泡给路由层（明确失败，不编造）。
    """
    retrieval = await retrieve_for_qa(
        session, member=member, project_id=project_id, query=query
    )
    if not retrieval.answerable:
        return QaAnswer(
            status="refused",
            answer=None,
            clues=await _to_sources(session, retrieval.clues),
        )

    sources = await _to_sources(session, retrieval.hits)
    snippets = "\n\n".join(
        f"[{i}] {s.snippet}" for i, s in enumerate(sources, start=1)
    )
    provider = get_model_provider()
    answer = await provider.generate(
        f"问题：{query}\n\n资料片段：\n{snippets}",
        system=_QA_SYSTEM,
    )
    return QaAnswer(status="answered", answer=answer.strip(), sources=sources)
