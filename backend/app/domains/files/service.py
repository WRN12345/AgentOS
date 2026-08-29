"""文件上传、下载应用服务与权限策略。

上传流程严格按序执行：流式写入 `StorageProvider.stage` 返回的暂存区，同时统计大小
并计算 SHA-256；通过大小、扩展名和 MIME 白名单校验后，由 `StorageProvider.commit`
原子移入正式存储，最后在同一事务内写入 `stored_files` 和审计事件。落库失败时
补偿删除已落盘文件。

项目负责人可下载全部文件；未关联工作项的知识库文档对项目内在职成员开放，
以保证检索结果可以查看原文。关联工作项的交付文件仅上传人或相关成员
可下载，避免无关成员读取交付文件正文。业务层只依赖 `StorageProvider` 接口，
不感知文件系统路径。
"""

import hashlib
import uuid
from pathlib import PurePosixPath

from fastapi import UploadFile
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ApiException, ErrorCodes
from app.core.logging import setup_logging
from app.domains.audit.service import record_event
from app.domains.collaboration.models import CollaborationRequest
from app.domains.files.models import StoredFile
from app.domains.files.schemas import StoredFileOut
from app.domains.memory.extractors import SUPPORTED_EXTENSIONS
from app.domains.memory.indexer import MEMORY_INDEX_TASK_TYPE, MemoryIndexService
from app.domains.project.models import ROLE_LEADER, ProjectMember
from app.domains.work_items.models import WorkItem, WorkItemCollaborator
from app.domains.work_items.service import get_work_item
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.queue.queue import enqueue
from app.infrastructure.storage.provider import StorageProvider

logger = setup_logging("backend")

UPLOAD_CHUNK_SIZE = 1024 * 1024

# 索引状态迁移表。
INDEX_PENDING = "pending"
INDEX_INDEXING = "indexing"
INDEX_INDEXED = "indexed"
INDEX_FAILED = "failed"
INDEX_UNINDEXED = "unindexed"

_INDEX_TRANSITIONS: dict[str, frozenset[str]] = {
    INDEX_PENDING: frozenset({INDEX_INDEXING, INDEX_UNINDEXED}),
    # 扫描版 PDF 的扩展名受支持，但提取时没有文字层，因此可从 `indexing` 转为 `unindexed`。
    INDEX_INDEXING: frozenset({INDEX_INDEXED, INDEX_FAILED, INDEX_UNINDEXED}),
    INDEX_FAILED: frozenset({INDEX_PENDING}),  # 失败任务允许手动重试。
    INDEX_INDEXED: frozenset(),
    INDEX_UNINDEXED: frozenset(),
}


def transition_index_status(stored: StoredFile, new_status: str) -> None:
    """校验索引状态迁移；非法迁移抛出 `ApiException`，由接口映射为 `409`。"""
    if new_status not in _INDEX_TRANSITIONS.get(stored.index_status, frozenset()):
        raise ApiException(
            409,
            ErrorCodes.FILE_INDEX_INVALID_TRANSITION,
            "索引状态不允许该迁移",
            details={"from": stored.index_status, "to": new_status},
        )
    stored.index_status = new_status


def _to_out(stored: StoredFile) -> StoredFileOut:
    return StoredFileOut(
        id=stored.id,
        project_id=stored.project_id,
        original_filename=stored.original_filename,
        size_bytes=stored.size_bytes,
        mime_type=stored.mime_type,
        sha256=stored.sha256,
        storage_backend=stored.storage_backend,
        uploaded_by=stored.uploaded_by,
        work_item_id=stored.work_item_id,
        version=stored.version,
        superseded_by=stored.superseded_by,
        index_status=stored.index_status,
        created_at=stored.created_at,
        updated_at=stored.updated_at,
    )


async def get_stored_file(
    session: AsyncSession, file_id: uuid.UUID, *, project_id: uuid.UUID | None = None
) -> StoredFile:
    stored = await session.get(StoredFile, file_id)
    if stored is None or (project_id is not None and stored.project_id != project_id):
        # 将跨项目访问视为文件不存在，避免泄露资源存在性。
        raise ApiException(404, ErrorCodes.NOT_FOUND, "文件不存在")
    return stored


def _validate_type(filename: str, content_type: str | None) -> str:
    """校验扩展名和声明的 MIME 类型，并移除文件名中的路径成分。"""
    safe_name = PurePosixPath(filename).name
    extension = PurePosixPath(safe_name).suffix.lower()
    if extension not in settings.allowed_upload_extensions:
        raise ApiException(
            415,
            ErrorCodes.FILE_TYPE_NOT_ALLOWED,
            "不支持的文件扩展名",
            details={"extension": extension},
        )
    if not content_type or content_type not in settings.allowed_upload_mime_types:
        raise ApiException(
            415,
            ErrorCodes.FILE_TYPE_NOT_ALLOWED,
            "不支持的文件类型",
            details={"mime_type": content_type or ""},
        )
    return safe_name or f"unnamed{extension}"


async def upload_file(
    session: AsyncSession,
    actor: ProjectMember,
    upload: UploadFile,
    work_item_id: uuid.UUID | None,
    provider: StorageProvider,
) -> StoredFileOut:
    """上传文件并可选关联同项目工作项。"""
    if work_item_id is not None:
        # 不存在或跨项目的工作项统一按 `404` 处理，避免泄露存在性。
        await get_work_item(session, work_item_id, project_id=actor.project_id)
    filename = _validate_type(upload.filename or "", upload.content_type)

    # 流式写入暂存区并同步计算大小和 SHA-256；超限立即中止并清理。
    staged = await provider.stage()
    hasher = hashlib.sha256()
    size = 0
    try:
        while chunk := await upload.read(UPLOAD_CHUNK_SIZE):
            size += len(chunk)
            if size > settings.upload_max_bytes:
                raise ApiException(
                    413,
                    ErrorCodes.FILE_TOO_LARGE,
                    "文件超过大小上限",
                    details={"max_bytes": settings.upload_max_bytes},
                )
            hasher.update(chunk)
            await staged.write(chunk)
    except BaseException:
        await provider.discard(staged)
        raise

    # 原子移入正式存储；随机后缀确保不同上传不会相互覆盖。
    sha256 = hasher.hexdigest()
    storage_key = f"{sha256[:2]}/{sha256}_{uuid.uuid4().hex[:12]}"
    await provider.commit(staged, storage_key)

    # 同项目同名文件形成新版本；旧版本保留并通过 `superseded_by` 指向新版本。
    current = (
        await session.execute(
            select(StoredFile).where(
                StoredFile.project_id == actor.project_id,
                StoredFile.original_filename == filename,
                StoredFile.superseded_by.is_(None),
            )
        )
    ).scalar_one_or_none()
    version = (current.version + 1) if current is not None else 1

    # 文件记录与审计同事务写入；数据库失败时补偿删除已落盘文件。部分唯一索引要求
    # 同名文件至多一个当前版本，因此先标记旧版本并 `flush`，再插入新版本。
    new_id = uuid.uuid4()
    stored = StoredFile(
        id=new_id,
        project_id=actor.project_id,
        storage_backend=provider.backend_name,
        storage_key=storage_key,
        original_filename=filename,
        size_bytes=size,
        mime_type=upload.content_type or "",
        sha256=sha256,
        uploaded_by=actor.id,
        work_item_id=work_item_id,
        version=version,
    )
    try:
        if current is not None:
            current.superseded_by = new_id
            await session.flush()
            # 同事务将旧版本的记忆块标记为失效，使检索只命中最新版本并保留历史追溯。
            await MemoryIndexService(session).mark_source_stale(
                source_type="document", source_id=current.id, commit=False
            )
        session.add(stored)
        await session.flush()
        await record_event(
            session,
            actor_id=actor.user_id,
            action="file.uploaded",
            target_type="stored_file",
            target_id=stored.id,
            before=None,
            after={
                "original_filename": filename,
                "size_bytes": size,
                "mime_type": stored.mime_type,
                "sha256": sha256,
                "storage_backend": provider.backend_name,
                "work_item_id": str(work_item_id) if work_item_id else None,
                "version": version,
                "supersedes": str(current.id) if current is not None else None,
            },
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        await provider.delete(storage_key)  # 补偿清理
        if "ux_stored_files_current_name" in str(exc):
            # 唯一索引阻止并发同名上传产生两个当前版本。
            raise ApiException(
                409,
                ErrorCodes.FILE_VERSION_CONFLICT,
                "同名文件正在并发上传，请稍后重试",
            ) from exc
        logger.warning("stored_files 落库失败，已补偿删除文件: storage_key=%s", storage_key)
        raise
    except BaseException:
        await session.rollback()
        await provider.delete(storage_key)  # 补偿清理
        logger.warning("stored_files 落库失败，已补偿删除文件: storage_key=%s", storage_key)
        raise

    await session.refresh(stored)  # created_at/updated_at 由数据库生成，刷新取回
    # 可读取内容的格式自动进入索引队列；其他格式标记为 `unindexed`，不影响上传。
    suffix = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if suffix in SUPPORTED_EXTENSIONS:
        await dispatch_index_task(stored)
    else:
        transition_index_status(stored, INDEX_UNINDEXED)
        await session.commit()
        await session.refresh(stored)
    logger.info(
        "file uploaded: id=%s, size=%d, sha256=%s, index_status=%s",
        stored.id, size, sha256, stored.index_status,
    )
    return _to_out(stored)




async def list_current_files(session: AsyncSession, actor: ProjectMember) -> list[StoredFileOut]:
    """返回项目成员可见的当前文件版本。"""
    rows = (
        await session.execute(
            select(StoredFile)
            .where(
                StoredFile.project_id == actor.project_id,
                StoredFile.superseded_by.is_(None),
            )
            .order_by(StoredFile.created_at.desc())
        )
    ).scalars()
    return [_to_out(row) for row in rows]


async def list_file_versions(
    session: AsyncSession, actor: ProjectMember, file_id: uuid.UUID
) -> list[StoredFileOut]:
    """按新到旧返回同名文件的全部版本。"""
    stored = await get_stored_file(session, file_id, project_id=actor.project_id)
    rows = (
        await session.execute(
            select(StoredFile)
            .where(
                StoredFile.project_id == actor.project_id,
                StoredFile.original_filename == stored.original_filename,
            )
            .order_by(StoredFile.version.desc())
        )
    ).scalars()
    return [_to_out(row) for row in rows]


async def dispatch_index_task(stored: StoredFile) -> None:
    """投递 `memory.index` 任务；投递失败时仅记录日志。"""
    redis_client = create_redis_client()
    try:
        await enqueue(
            redis_client,
            MEMORY_INDEX_TASK_TYPE,
            {
                "project_id": str(stored.project_id),
                "source_type": "document",
                "source_id": str(stored.id),
                "stored_file_id": str(stored.id),
            },
        )
    except Exception:  # noqa: BLE001 - 任务投递失败不拖垮主流程，状态停在 pending
        logger.warning("index task enqueue failed: file=%s", stored.id, exc_info=True)
    finally:
        await redis_client.aclose()


async def retry_file_index(
    session: AsyncSession, actor: ProjectMember, file_id: uuid.UUID
) -> StoredFileOut:
    """将 `failed` 重置为 `pending` 并重新投递索引任务。"""
    stored = await get_stored_file(session, file_id, project_id=actor.project_id)
    transition_index_status(stored, INDEX_PENDING)
    await session.commit()
    await session.refresh(stored)
    await dispatch_index_task(stored)
    logger.info("file index retry dispatched: id=%s", stored.id)
    return _to_out(stored)




async def is_work_item_related(
    session: AsyncSession, work_item_id: uuid.UUID, member_id: uuid.UUID
) -> bool:
    """判断成员是否为主执行人、协作者或协作请求任一方。"""
    item = await session.get(WorkItem, work_item_id)
    if item is None:
        return False
    if item.assignee_id == member_id:
        return True
    collaborator = (
        await session.execute(
            select(WorkItemCollaborator.id)
            .where(
                WorkItemCollaborator.work_item_id == work_item_id,
                WorkItemCollaborator.member_id == member_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if collaborator is not None:
        return True
    party = (
        await session.execute(
            select(CollaborationRequest.id)
            .where(
                CollaborationRequest.work_item_id == work_item_id,
                or_(
                    CollaborationRequest.requester_id == member_id,
                    CollaborationRequest.assignee_id == member_id,
                ),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return party is not None


async def can_download_file(
    session: AsyncSession, actor: ProjectMember, stored: StoredFile
) -> bool:
    """负责人和上传人可下载；未关联工作项的知识库文档对项目成员开放；
    关联工作项的交付文件仅上传人或与工作项有关的成员可下。"""
    if actor.role in (ROLE_LEADER):
        return True
    if stored.uploaded_by == actor.id:
        return True
    if stored.work_item_id is None:
        return True
    return await is_work_item_related(session, stored.work_item_id, actor.id)


async def authorize_download(
    session: AsyncSession,
    actor: ProjectMember,
    file_id: uuid.UUID,
    provider: StorageProvider,
) -> StoredFile:
    """下载前依次校验文件存在性、成员权限并写审计。

    记录或物理文件不存在时返回 `404`，无关成员返回 `403`。审计事件包含操作者、
    目标文件、请求 ID 和来源 IP。
    """
    stored = await get_stored_file(session, file_id, project_id=actor.project_id)
    if not await can_download_file(session, actor, stored):
        raise ApiException(403, ErrorCodes.FORBIDDEN, "无权下载该文件")
    if stored.storage_backend != provider.backend_name or not await provider.exists(
        stored.storage_key
    ):
        raise ApiException(404, ErrorCodes.NOT_FOUND, "文件不存在或已清理")
    await record_event(
        session,
        actor_id=actor.user_id,
        action="file.downloaded",
        target_type="stored_file",
        target_id=stored.id,
        before=None,
        after={
            "original_filename": stored.original_filename,
            "sha256": stored.sha256,
            "work_item_id": str(stored.work_item_id) if stored.work_item_id else None,
        },
    )
    await session.commit()
    return stored
