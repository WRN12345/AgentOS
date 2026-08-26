"""记忆检索核心（设计文档第 5 节）：query → embedding → pgvector 余弦相似度 top-k。

命中口径（多处共用）：
- 只命中 is_current=True 的块（旧版本文档内容不参与检索，第 3 节）；
- 只命中当前 EMBEDDING_MODEL 生成的向量（16.4，换模型全量重建前的旧向量不混入）；
- project_id 严格相等——项目隔离在检索层强制（第 12 节），成员档案
  （project_id 为 NULL）的跨项目放行是独立规则，在 M3.9 的权限层处理；
- 余弦距离超过 max_distance 的结果丢弃（16.13 拒答策略的底层依据）。

权限说明：本服务只做"项目内命中"，调用方鉴权（谁能搜哪个项目）在 M2.9/M2.10；
Agent 与问答页面必须共用同一条带权限校验的路径（第 12 节）。
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.domains.memory.models import MemoryChunk
from app.infrastructure.models.embedding import EmbeddingProvider, get_embedding_provider


@dataclass(frozen=True)
class RetrievalResult:
    """一条命中：块内容 + 来源定位（供问答页展示依据、Agent 引用）。"""

    chunk_id: uuid.UUID
    source_type: str
    source_id: uuid.UUID
    content: str
    distance: float


class MemoryRetriever:
    def __init__(
        self,
        session: AsyncSession,
        provider: EmbeddingProvider | None = None,
    ) -> None:
        self._session = session
        self._provider = provider or get_embedding_provider()

    async def search(
        self,
        query: str,
        *,
        project_id: uuid.UUID,
        source_types: list[str] | None = None,
        limit: int | None = None,
        max_distance: float | None = None,
        include_cross_project_profiles: bool = False,
    ) -> list[RetrievalResult]:
        """项目内向量检索，按余弦距离升序返回 top-k（超过距离上限的丢弃）。

        include_cross_project_profiles=True 时（仅 leader_query / agent_assignment
        两个调用方，16.12），额外命中 project_id 为 NULL 的 profile 块——
        档案随人走、跨项目可见的唯一例外；是否放行由 search.py 权限层判定。
        """
        if not query.strip():
            return []
        limit = limit if limit is not None else settings.memory_search_limit
        max_distance = (
            max_distance if max_distance is not None else settings.memory_search_max_distance
        )

        query_vector = (await self._provider.embed([query]))[0]
        distance = MemoryChunk.embedding.cosine_distance(query_vector).label("distance")
        if include_cross_project_profiles:
            project_filter = or_(
                MemoryChunk.project_id == project_id,
                and_(
                    MemoryChunk.project_id.is_(None),
                    MemoryChunk.source_type == "profile",
                ),
            )
        else:
            project_filter = MemoryChunk.project_id == project_id
        stmt = (
            select(MemoryChunk, distance)
            .where(
                project_filter,
                MemoryChunk.is_current.is_(True),
                MemoryChunk.model_version == settings.embedding_model,
            )
            .order_by(distance)
            .limit(limit)
        )
        if source_types:
            stmt = stmt.where(MemoryChunk.source_type.in_(source_types))

        rows = (await self._session.execute(stmt)).all()
        return [
            RetrievalResult(
                chunk_id=chunk.id,
                source_type=chunk.source_type,
                source_id=chunk.source_id,
                content=chunk.content,
                distance=float(dist),
            )
            for chunk, dist in rows
            if float(dist) <= max_distance
        ]
