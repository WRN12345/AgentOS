"""记忆索引任务（M1.8/M2.6，设计文档第 3、6、13 节）。

任务类型 `memory.index`，payload 两种形态：
- 文档索引（M2.6）：`stored_file_id`（必带）+ `project_id` / `source_type` / `source_id`，
  任务负责"读文件 → 提取文本 → 切块入库 → 驱动索引状态机"全流程；
- 纯文本索引（M1.8 桩，档案/历史/核心记忆复用）：`project_id` / `source_type` /
  `source_id` / `text`，直接切块入库；
- `attempt`：已重试次数（重入队时自增）。

失败语义（第 6 节）：
- 确定性失败（文件损坏、扫描件 PDF）不重试，直接驱动状态机到 failed/unindexed；
- 瞬态失败（embedding 不可用、DB 瞬断）按指数退避重入队，最多 MAX_ATTEMPTS 次；
  耗尽后文档状态标记 failed（可人工重试，M2.4）。
"""

import uuid

import redis.asyncio as redis

from app.core.logging import setup_logging
from app.domains.files.models import StoredFile
from app.domains.files.service import (
    INDEX_FAILED,
    INDEX_INDEXED,
    INDEX_INDEXING,
    INDEX_PENDING,
    INDEX_UNINDEXED,
    transition_index_status,
)
from app.domains.memory.extractors import (
    ExtractionFailedError,
    UnsupportedFormatError,
    extract_text,
)
from app.domains.memory.history import build_run_history_text
from app.domains.memory.indexer import MEMORY_INDEX_TASK_TYPE, MemoryIndexService
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.queue.queue import enqueue_delayed
from app.infrastructure.storage.provider import get_storage_provider

logger = setup_logging("worker.memory_index")

TASK_TYPE = MEMORY_INDEX_TASK_TYPE
#: 最大重试次数（不含首次执行）与指数退避基数（秒）：10s → 20s → 40s
MAX_ATTEMPTS = 3
RETRY_BASE_SECONDS = 10.0


async def _index_stored_file(file_id: uuid.UUID) -> int:
    """文档索引全流程（M2.6）：返回写入的块数。确定性失败不抛出（不重试）。"""
    async with async_session_factory() as session:
        stored = await session.get(StoredFile, file_id)
        if stored is None:
            logger.warning("index target file not found, skipped: %s", file_id)
            return 0
        if stored.index_status in (INDEX_INDEXED, INDEX_UNINDEXED):
            return 0  # 幂等：终态不重复处理（重复投递/新版本已取代）
        if stored.index_status == INDEX_PENDING:
            transition_index_status(stored, INDEX_INDEXING)
            await session.commit()
        # 状态为 indexing 时说明是任务重试，直接继续

        provider = get_storage_provider()
        data = await provider.load(stored.storage_key)
        try:
            text = extract_text(stored.original_filename, data)
        except UnsupportedFormatError:
            # 扫描件 PDF 等：扩展名支持但读不出内容 → unindexed（终态，不重试）
            transition_index_status(stored, INDEX_UNINDEXED)
            await session.commit()
            logger.info("file has no extractable content, unindexed: %s", file_id)
            return 0
        except ExtractionFailedError:
            # 文件损坏：failed，留待人工重试（M2.4），任务层面不再重投
            transition_index_status(stored, INDEX_FAILED)
            await session.commit()
            logger.warning("file content extraction failed: %s", file_id, exc_info=True)
            return 0

        service = MemoryIndexService(session)
        written = await service.rebuild_chunks(
            project_id=stored.project_id,
            source_type="document",
            source_id=stored.id,
            text=text,
        )
        if stored.superseded_by is not None:
            # 索引完成时该版本已被新版本取代（竞态：上传在索引途中发生）：
            # 块写入后立即标失效，保证检索永远只命中最新版本（设计文档第 3 节）
            await service.mark_source_stale(source_type="document", source_id=stored.id)
        transition_index_status(stored, INDEX_INDEXED)
        await session.commit()
        return written


async def _mark_file_failed(file_id: uuid.UUID) -> None:
    """重试耗尽后把文档标记为 failed（best-effort，失败只记日志）。"""
    try:
        async with async_session_factory() as session:
            stored = await session.get(StoredFile, file_id)
            if stored is not None and stored.index_status == INDEX_INDEXING:
                transition_index_status(stored, INDEX_FAILED)
                await session.commit()
    except Exception:  # noqa: BLE001
        logger.error("failed to mark file index failed: %s", file_id, exc_info=True)


async def execute_memory_index(payload: dict, redis_client: redis.Redis) -> None:
    source_type = payload.get("source_type", "<unknown>")
    source_id = payload.get("source_id", "<unknown>")
    attempt = int(payload.get("attempt", 0))
    stored_file_id = payload.get("stored_file_id")
    try:
        if stored_file_id:
            written = await _index_stored_file(uuid.UUID(str(stored_file_id)))
        else:
            # 纯文本路径：档案/核心记忆直接携带文本；history 类型由 worker 从
            # run 记录现取文本（M5.1），保证块内容反映最新采纳状态
            project_id = (
                uuid.UUID(payload["project_id"]) if payload.get("project_id") else None
            )
            async with async_session_factory() as session:
                service = MemoryIndexService(session)
                if source_type == "history" and "text" not in payload:
                    text = await build_run_history_text(session, uuid.UUID(str(source_id)))
                    if text is None:
                        # 运行未完成/非拆解分配类型：不索引（15.4）
                        logger.info("history source not indexable, skipped: %s", source_id)
                        return
                else:
                    text = str(payload.get("text", ""))
                written = await service.rebuild_chunks(
                    project_id=project_id,
                    source_type=str(source_type),
                    source_id=uuid.UUID(str(source_id)),
                    text=text,
                )
        logger.info(
            "memory index done: type=%s id=%s chunks=%d", source_type, source_id, written
        )
    except Exception:
        if attempt < MAX_ATTEMPTS:
            delay = RETRY_BASE_SECONDS * (2**attempt)
            await enqueue_delayed(
                redis_client,
                TASK_TYPE,
                {**payload, "attempt": attempt + 1},
                delay_seconds=delay,
            )
            logger.warning(
                "memory index failed, retry %d/%d in %.0fs: type=%s id=%s",
                attempt + 1,
                MAX_ATTEMPTS,
                delay,
                source_type,
                source_id,
                exc_info=True,
            )
        else:
            logger.error(
                "memory index failed after %d retries, dropped: type=%s id=%s",
                MAX_ATTEMPTS,
                source_type,
                source_id,
                exc_info=True,
            )
            if stored_file_id:
                await _mark_file_failed(uuid.UUID(str(stored_file_id)))
