"""将来源文本切块并生成 `embedding`，统一写入 `memory_chunks`。

`document`、`profile`、`history` 和 `core_memory` 共用此服务。重建按来源先删除后写入，
并通过来源级事务锁串行化。模型版本取当前 `EMBEDDING_MODEL`；生成向量失败时异常交由
`worker` 重试或由 `Agent` 调用方降级处理。
"""

import uuid

from sqlalchemy import delete, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.domains.memory.chunking import chunk_text
from app.domains.memory.models import SOURCE_TYPES, MemoryChunk
from app.infrastructure.models.embedding import EmbeddingProvider, get_embedding_provider

MEMORY_INDEX_TASK_TYPE = "memory.index"

# 同一来源的删除和重写必须串行，避免租约重投与原任务并发写出重复的当前块。
# `pg_advisory_xact_lock` 随事务结束自动释放，并由唯一约束提供最终防重保障。
_SOURCE_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))")


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
        """整体重建一个来源的记忆块，并返回写入数量。

        只有 `profile` 的 `project_id` 可以为空；空文本只删除旧块，不写入新块。
        """
        if source_type not in SOURCE_TYPES:
            raise ValueError(f"未知记忆来源类型: {source_type}")
        if source_type != "profile" and project_id is None:
            raise ValueError(f"{source_type} 类型必须带 project_id")

        # 锁覆盖删除和重写的完整临界区
        await self._session.execute(
            _SOURCE_LOCK_SQL, {"lock_key": f"memory_index:{source_type}:{source_id}"}
        )
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
                    chunk_index=index,
                    content=content,
                    embedding=vector,
                    model_version=settings.embedding_model,
                )
                for index, (content, vector) in enumerate(zip(chunks, vectors, strict=True))
            ]
        )
        await self._session.commit()
        return len(chunks)

    async def mark_source_stale(
        self,
        *,
        source_type: str,
        source_id: uuid.UUID,
        commit: bool = True,
    ) -> int:
        """将来源的全部块标记为失效，并返回影响行数。

        旧版本块不再参与检索，但继续保留供追溯。
        """
        result = await self._session.execute(
            update(MemoryChunk)
            .where(
                MemoryChunk.source_type == source_type,
                MemoryChunk.source_id == source_id,
                MemoryChunk.is_current.is_(True),
            )
            .values(is_current=False)
        )
        if commit:
            await self._session.commit()
        return int(result.rowcount)  # type: ignore[attr-defined]
