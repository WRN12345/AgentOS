"""记忆索引任务（M1.8/M2.6，设计文档第 3、6、13 节）。

任务类型 `memory.index`，payload 两种形态：
- 文档索引（M2.6）：`stored_file_id`（必带）+ `project_id` / `source_type` / `source_id`，
  任务负责"读文件 → 提取文本 → 切块入库 → 驱动索引状态机"全流程；
- 纯文本索引：`history` 可携带 `text`，`profile` 与 `core_memory` 仅携带来源 ID，
  worker 在执行时读取当前数据库内容，避免延迟任务覆盖后续编辑；
- `attempt`：已重试次数（重入队时自增）。

失败语义（第 6 节）：
- 确定性失败（文件损坏、扫描件 PDF）不重试，直接驱动状态机到 failed/unindexed；
- 瞬态失败（embedding 不可用、DB 瞬断）按指数退避重入队，最多 MAX_ATTEMPTS 次；
  耗尽后文档状态标记 failed（可人工重试，M2.4）。
"""

import uuid
from datetime import UTC, datetime, timedelta

import redis.asyncio as redis
from sqlalchemy import and_, or_, select, update

from app.core.config import settings
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
from app.domains.memory.core_memory import invalidate_core_memory_index
from app.domains.memory.extractors import (
    ExtractionFailedError,
    UnsupportedFormatError,
    extract_text,
)
from app.domains.memory.history import (
    HISTORY_KIND_WORK_ITEM,
    build_run_history_text,
    build_work_item_conclusion_text,
)
from app.domains.memory.models import CoreMemoryEntry, MemberProfile
from app.domains.memory.indexer import MEMORY_INDEX_TASK_TYPE, MemoryIndexService
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.queue.queue import enqueue, enqueue_delayed
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
            # 原子认领（并发重复任务，如租约重投与原任务并存）：仅状态仍为
            # pending 才置 indexing；rowcount=0 说明另一 worker 已认领，跳过。
            # 块写入另有 rebuild_chunks 的来源级互斥与唯一约束兜底。
            claimed = await session.execute(
                update(StoredFile)
                .where(
                    StoredFile.id == file_id,
                    StoredFile.index_status == INDEX_PENDING,
                )
                .values(index_status=INDEX_INDEXING, index_started_at=datetime.now(UTC))
            )
            if int(claimed.rowcount) == 0:  # type: ignore[attr-defined]
                logger.info("index task already claimed by another worker: %s", file_id)
                return 0
            await session.commit()
            # 原子 UPDATE 不刷新 ORM 对象（会话禁用 expire_on_commit），
            # 显式刷新使后续 transition_index_status 校验基于真实状态
            await session.refresh(stored)
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
        # rebuild_chunks() 已提交块写入；会话禁用了 expire_on_commit，必须显式刷新，
        # 否则索引开始后被新版本替代时仍会读到缓存的 superseded_by=None。
        await session.refresh(stored, attribute_names=["superseded_by"])
        if stored.superseded_by is not None:
            # 索引完成时该版本已被新版本取代（竞态：上传在索引途中发生）：
            # 块写入后立即标失效，保证检索永远只命中最新版本（设计文档第 3 节）
            await service.mark_source_stale(source_type="document", source_id=stored.id)
        transition_index_status(stored, INDEX_INDEXED)
        await session.commit()
        return written


async def recover_stale_file_indexes(
    redis_client: redis.Redis,
    *,
    now: datetime | None = None,
) -> int:
    """恢复超过租约的索引任务并重新投递，返回恢复数量。

    覆盖两类滞留：
    - indexing 超租约：worker 中断遗留。新代码写入 index_started_at；迁移前
      异常遗留的 indexing 记录没有该字段，使用其 updated_at 作为保守回退；
    - pending 超租约：上传提交后首次投递失败（Redis 短暂不可用）遗留，
      updated_at 即上传时间；重投成功后以 index_started_at 记录上次投递时间，
      避免每个扫描周期重复入队，worker 消费时会覆写为真正的索引开始时间。

    重复投递是安全的：索引消费对终态幂等跳过，indexing 状态下重建块。
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=settings.file_index_lease_seconds)
    async with async_session_factory() as session:
        stale_files = list(
            (
                await session.execute(
                    select(StoredFile)
                    .where(
                        StoredFile.index_status.in_((INDEX_INDEXING, INDEX_PENDING)),
                        or_(
                            StoredFile.index_started_at < cutoff,
                            and_(
                                StoredFile.index_started_at.is_(None),
                                StoredFile.updated_at < cutoff,
                            ),
                        ),
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        recovered = 0
        for stored in stale_files:
            try:
                await enqueue(
                    redis_client,
                    TASK_TYPE,
                    {
                        "project_id": str(stored.project_id),
                        "source_type": "document",
                        "source_id": str(stored.id),
                        "stored_file_id": str(stored.id),
                    },
                )
            except Exception:  # noqa: BLE001 - 保留过期租约，下一轮继续尝试
                logger.exception("failed to requeue stale file index task: file=%s", stored.id)
                continue
            # 只有任务已入队才续租。入队失败时保留过期时间，避免 pending 无任务。
            stored.index_started_at = now
            recovered += 1
        await session.commit()

    if recovered:
        logger.warning("recovered stale file index tasks: count=%d", recovered)
    return recovered


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
            # history 由 worker 从运行记录现取文本；profile/core_memory 也从来源
            # 实体读取当前内容，避免延迟重试使用旧快照覆盖后续编辑。
            project_id = (
                uuid.UUID(payload["project_id"]) if payload.get("project_id") else None
            )
            async with async_session_factory() as session:
                service = MemoryIndexService(session)
                if source_type == "core_memory":
                    entry = await session.get(CoreMemoryEntry, uuid.UUID(str(source_id)))
                    if entry is None or entry.project_id != project_id:
                        logger.info("core memory source not found, skipped: %s", source_id)
                        return
                    if entry.status != "active":
                        await invalidate_core_memory_index(session, entry.id)
                        written = 0
                    else:
                        written = await service.rebuild_chunks(
                            project_id=project_id,
                            source_type="core_memory",
                            source_id=entry.id,
                            text=entry.content,
                        )
                else:
                    if source_type == "profile":
                        # 忽略旧任务可能携带的 text 快照；始终索引当前档案内容。
                        profile = await session.get(MemberProfile, uuid.UUID(str(source_id)))
                        if profile is None:
                            logger.info("member profile source not found, skipped: %s", source_id)
                            return
                        text = profile.content
                    elif source_type == "history" and "text" not in payload:
                        if payload.get("history_kind") == HISTORY_KIND_WORK_ITEM:
                            text = await build_work_item_conclusion_text(
                                session, uuid.UUID(str(source_id))
                            )
                        else:
                            text = await build_run_history_text(
                                session, uuid.UUID(str(source_id))
                            )
                        if text is None:
                            # 运行/工作项未完成：不索引（15.4）
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
