"""索引写入服务（设计文档第 5、13 节）：source 文本 → 切块 → embedding → memory_chunks。

四类记忆（document/profile/history/core_memory）共用本服务：
- rebuild_chunks 整体重建某来源的块（先删旧块再写入），保证内容变更后索引一致；
- model_version 取当前 EMBEDDING_MODEL（16.4），检索侧只命中当前版本；
- embedding 不可用时 ModelError 冒泡给调用方（worker 索引任务据此重试/标失败，
  Agent 侧据此降级为无记忆模式，16.5）。
"""

import uuid

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.domains.memory.chunking import chunk_text
from app.domains.memory.models import SOURCE_TYPES, MemoryChunk
from app.infrastructure.models.embedding import EmbeddingProvider, get_embedding_provider


class MemoryIndexService:
    def __init__(
        self,
        session: AsyncSession,
        provider: EmbeddingProvider | None = None,
    ) -> None:
        self._session = session
        self._provider = provider or get_embedding_provider()

    async def rebuild_chunks(
        self,
        *,
        project_id: uuid.UUID | None,
        source_type: str,
        source_id: uuid.UUID,
        text: str,
    ) -> int:
        """整体重建某来源的记忆块，返回写入的块数。

        - project_id：仅 profile 类型传 None（随人走，16.12），其余类型必填；
        - 空文本只删旧块不写新块（来源内容被清空的语义）。
        """
        if source_type not in SOURCE_TYPES:
            raise ValueError(f"未知记忆来源类型: {source_type}")
        if source_type != "profile" and project_id is None:
            raise ValueError(f"{source_type} 类型必须带 project_id")

        await self._session.execute(
            delete(MemoryChunk).where(
                MemoryChunk.source_type == source_type,
                MemoryChunk.source_id == source_id,
            )
        )

        chunks = chunk_text(text)
        if not chunks:
            await self._session.commit()
            return 0

        vectors = await self._provider.embed(chunks)
        self._session.add_all(
            [
                MemoryChunk(
                    project_id=project_id,
                    source_type=source_type,
                    source_id=source_id,
                    content=content,
                    embedding=vector,
                    model_version=settings.embedding_model,
                )
                for content, vector in zip(chunks, vectors, strict=True)
            ]
        )
        await self._session.commit()
        return len(chunks)
