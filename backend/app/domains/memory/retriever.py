"""基于 `pgvector` 余弦距离的记忆检索核心。

检索只使用当前模型生成的有效块，并在查询层强制项目隔离。跨项目成员档案仅在上层
权限明确放行时加入。超过 `max_distance` 的结果会被丢弃。此服务不鉴权，调用方必须
通过 `search_memory` 权限层进入。
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
    """包含块内容、来源定位和余弦距离的一条命中。"""

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
        """按余弦距离升序返回项目内的 `top-k` 命中。

        `include_cross_project_profiles=True` 时额外包含不绑定项目的 `profile` 块；
        是否允许该选项必须由 `search.py` 权限层判定。
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
