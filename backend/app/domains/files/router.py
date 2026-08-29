"""文件接口。

下载接口在鉴权后流式返回文件，也是唯一文件出口。上传目录不通过静态服务或
反向代理暴露，响应头携带规范的文件名与 `Content-Type`。
"""

import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.idempotency import idempotency_guard
from app.domains.files.schemas import StoredFileOut
from app.domains.files.service import (
    authorize_download,
    list_current_files,
    list_file_versions,
    retry_file_index,
    upload_file,
)
from app.domains.project.dependencies import get_current_member
from app.domains.project.models import ProjectMember
from app.infrastructure.database.engine import get_session
from app.infrastructure.storage.provider import StorageProvider, get_storage_provider

router = APIRouter(tags=["files"])


def _content_disposition(filename: str) -> str:
    """按 RFC 5987 编码：非 ASCII 文件名使用 `filename*`，同时提供 ASCII `filename`。"""
    fallback = filename.encode("ascii", "replace").decode().replace('"', "_")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"


@router.post("/files", response_model=StoredFileOut, status_code=201)
async def upload_file_endpoint(
    file: UploadFile = File(...),
    work_item_id: uuid.UUID | None = Form(None),
    actor: ProjectMember = Depends(get_current_member),
    _: None = Depends(idempotency_guard),
    provider: StorageProvider = Depends(get_storage_provider),
    session: AsyncSession = Depends(get_session),
) -> StoredFileOut:
    return await upload_file(session, actor, file, work_item_id, provider)


@router.get("/files", response_model=list[StoredFileOut])
async def list_files_endpoint(
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> list[StoredFileOut]:
    """返回项目内文件的当前版本。"""
    return await list_current_files(session, actor)


@router.get("/files/{file_id}/versions", response_model=list[StoredFileOut])
async def list_file_versions_endpoint(
    file_id: uuid.UUID,
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> list[StoredFileOut]:
    """按新到旧返回同名文件的版本历史。"""
    return await list_file_versions(session, actor, file_id)


@router.post("/files/{file_id}/index-retry", response_model=StoredFileOut)
async def retry_file_index_endpoint(
    file_id: uuid.UUID,
    actor: ProjectMember = Depends(get_current_member),
    session: AsyncSession = Depends(get_session),
) -> StoredFileOut:
    """将失败索引重置为 `pending` 并重新投递任务。"""
    return await retry_file_index(session, actor, file_id)


@router.get("/files/{file_id}/download")
async def download_file_endpoint(
    file_id: uuid.UUID,
    actor: ProjectMember = Depends(get_current_member),
    provider: StorageProvider = Depends(get_storage_provider),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    stored = await authorize_download(session, actor, file_id, provider)
    return StreamingResponse(
        provider.iter_chunks(stored.storage_key),
        media_type=stored.mime_type,
        headers={"Content-Disposition": _content_disposition(stored.original_filename)},
    )
