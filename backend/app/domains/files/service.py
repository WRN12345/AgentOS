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
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ApiException, ErrorCodes
from app.core.logging import setup_logging
from app.domains.audit.service import record_event
from app.domains.collaboration.models import CollaborationRequest
from app.domains.files.models import StoredFile
from app.domains.files.schemas import StoredFileOut
from app.domains.project.models import ROLE_ADMIN, ROLE_LEADER, ProjectMember
from app.domains.work_items.models import WorkItem, WorkItemCollaborator
from app.domains.work_items.service import get_work_item
from app.infrastructure.storage.provider import StorageProvider

logger = setup_logging("backend")

UPLOAD_CHUNK_SIZE = 1024 * 1024


def _to_out(stored: StoredFile) -> StoredFileOut:
    return StoredFileOut(
        id=stored.id,
        original_filename=stored.original_filename,
        size_bytes=stored.size_bytes,
        mime_type=stored.mime_type,
        sha256=stored.sha256,
        storage_backend=stored.storage_backend,
        uploaded_by=stored.uploaded_by,
        work_item_id=stored.work_item_id,
        created_at=stored.created_at,
        updated_at=stored.updated_at,
    )


async def get_stored_file(session: AsyncSession, file_id: uuid.UUID) -> StoredFile:
    stored = await session.get(StoredFile, file_id)
    if stored is None:
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
    """上传文件（14 章）：要求登录态；可选关联工作项（须存在，T4.4 复用）。"""
    if work_item_id is not None:
        await get_work_item(session, work_item_id)  # 不存在 → 404
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

    # 5) 同事务写 stored_files 与审计事件；失败则补偿删除已落盘文件（17.2 节）
    stored = StoredFile(
        storage_backend=provider.backend_name,
        storage_key=storage_key,
        original_filename=filename,
        size_bytes=size,
        mime_type=upload.content_type or "",
        sha256=sha256,
        uploaded_by=actor.id,
        work_item_id=work_item_id,
    )
    session.add(stored)
    try:
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
            },
        )
        await session.commit()
    except BaseException:
        await session.rollback()
        await provider.delete(storage_key)  # 补偿清理
        logger.warning("stored_files 落库失败，已补偿删除文件: storage_key=%s", storage_key)
        raise

    await session.refresh(stored)  # created_at/updated_at 由数据库生成，刷新取回
    logger.info("file uploaded: id=%s, size=%d, sha256=%s", stored.id, size, sha256)
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
    if actor.role in (ROLE_LEADER, ROLE_ADMIN):
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
    stored = await get_stored_file(session, file_id)
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
