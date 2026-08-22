"""文件上传/下载应用服务与权限策略（12.5、14、16、17.2 节）。

上传流程（14 章，严格按序）：
1) 流式写入暂存文件（Provider.stage），边写边计大小；
2) 校验大小上限、扩展名与 MIME 白名单（配置项，T1.2 配置模块）；
3) 流式过程中计算 SHA-256；
4) Provider.commit 原子移动到正式目录；
5) 同一事务写入 stored_files 与审计事件。
落库失败时补偿删除已落盘文件（17.2 节）。

下载权限（16 节）：项目负责人可下载全部文件；其他成员须为上传人本人，
或与文件关联工作项有关（主执行人、协作者、协作请求任一方）；其余 403。
业务层只依赖 StorageProvider 接口，不感知文件系统路径（14 章）。
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

# 索引状态机（设计文档第 6 节，迁移 0025）
INDEX_PENDING = "pending"
INDEX_INDEXING = "indexing"
INDEX_INDEXED = "indexed"
INDEX_FAILED = "failed"
INDEX_UNINDEXED = "unindexed"

_INDEX_TRANSITIONS: dict[str, frozenset[str]] = {
    INDEX_PENDING: frozenset({INDEX_INDEXING, INDEX_UNINDEXED}),
    # indexing → unindexed：提取时才发现是扫描件 PDF（扩展名支持但无文字层）
    INDEX_INDEXING: frozenset({INDEX_INDEXED, INDEX_FAILED, INDEX_UNINDEXED}),
    INDEX_FAILED: frozenset({INDEX_PENDING}),  # 手动重试
    INDEX_INDEXED: frozenset(),
    INDEX_UNINDEXED: frozenset(),
}


def transition_index_status(stored: StoredFile, new_status: str) -> None:
    """索引状态机校验（设计文档第 6 节）：非法迁移抛 409。"""
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
        # 越权访问他项目文件与文件不存在等价，不暴露存在性（spec D5 越权 404）
        raise ApiException(404, ErrorCodes.NOT_FOUND, "文件不存在")
    return stored


def _validate_type(filename: str, content_type: str | None) -> str:
    """校验扩展名与声明的 MIME 类型均在白名单内，返回规范化的文件名（去掉路径成分）。"""
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
    """上传文件（14 章）：要求登录态；可选关联工作项（须存在且同项目，T4.4 复用）。"""
    if work_item_id is not None:
        # 跨实体引用同项目校验：不存在或他项目工作项 → 404（spec D5）
        await get_work_item(session, work_item_id, project_id=actor.project_id)
    filename = _validate_type(upload.filename or "", upload.content_type)

    # 1-3) 流式写暂存文件，边写边计大小与 SHA-256；超限即中止并清理暂存
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

    # 4) 原子移动到正式目录；键含随机后缀，不同上传永不互相覆盖
    sha256 = hasher.hexdigest()
    storage_key = f"{sha256[:2]}/{sha256}_{uuid.uuid4().hex[:12]}"
    await provider.commit(staged, storage_key)

    # 5) 版本链（设计文档第 3 节）：同项目同名文件 = 新版本；
    # 旧版本保留并将 superseded_by 指向新版本，检索只命中最新版本（M2.7）
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

    # 6) 同事务写 stored_files 与审计事件；失败则补偿删除已落盘文件（17.2 节）
    # 注意顺序：部分唯一索引要求"同名至多一个当前版本"，必须先把旧版本标记为
    # 被取代并 flush，再插入新版本行，否则 INSERT 瞬间存在两个当前版本
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
            # 旧版本的记忆块同步失效（设计文档第 3 节）：检索只命中最新版本，
            # 旧块保留供人工追溯。同事务标记，覆盖旧版本已索引完成的情况
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
            # 并发同名上传撞唯一索引（设计文档第 3 节）：提示重试，不产生两个"最新版"
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
    # 上传即入库（设计文档第 3、6 节）：支持读取内容的格式自动进入索引队列；
    # 其余格式（zip/图片等）直接标 unindexed，不影响上传与其他文件
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


# ---------- 索引（设计文档第 6 节） ----------


async def list_current_files(session: AsyncSession, actor: ProjectMember) -> list[StoredFileOut]:
    """项目内当前版本文件列表（设计文档第 12 节：项目内全员可见）。"""
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
    """同名文档的全部版本（新→旧），供版本历史查看（设计文档第 3 节）。"""
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
    """投递 memory.index 索引任务（上传即入库 / 失败重试共用）；投递失败只记日志。"""
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
    """索引失败手动重试（设计文档第 6 节）：failed → pending 并重投索引任务。"""
    stored = await get_stored_file(session, file_id, project_id=actor.project_id)
    transition_index_status(stored, INDEX_PENDING)
    await session.commit()
    await session.refresh(stored)
    await dispatch_index_task(stored)
    logger.info("file index retry dispatched: id=%s", stored.id)
    return _to_out(stored)


# ---------- 下载权限（16 节） ----------


async def is_work_item_related(
    session: AsyncSession, work_item_id: uuid.UUID, member_id: uuid.UUID
) -> bool:
    """用户是否与工作项有关：主执行人、协作者、协作请求任一方（16 节）。

    T4.4（交付物提交权限）与 T4.5（审核意见可见性）复用本辅助函数。
    """
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
    """下载权限（16 节）：负责人、管理员（只读）全部可下；上传人本人可下；
    关联工作项的文件须与工作项有关；未关联文件仅负责人/管理员与上传人。"""
    if actor.role in (ROLE_LEADER):
        return True
    if stored.uploaded_by == actor.id:
        return True
    if stored.work_item_id is None:
        return False
    return await is_work_item_related(session, stored.work_item_id, actor.id)


async def authorize_download(
    session: AsyncSession,
    actor: ProjectMember,
    file_id: uuid.UUID,
    provider: StorageProvider,
) -> StoredFile:
    """下载前鉴权：404（记录或物理文件不存在/已清理）→ 403（无关成员）→ 写审计。

    审计事件含操作者与目标文件；request_id 与来源 IP 由请求上下文自动带（16 节）。
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
