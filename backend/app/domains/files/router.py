"""文件接口（12.5 节）。

- POST /files                    任何登录成员：multipart 上传（可选 work_item_id 关联）
- GET  /files/{id}/download      鉴权 + 权限校验（16 节）后由后端流式返回

下载是唯一文件出口：上传目录不经静态服务/反向代理暴露（14 章），
响应头携带规范的文件名与 Content-Type。
"""

import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.idempotency import idempotency_guard
from app.domains.files.schemas import StoredFileOut
from app.domains.files.service import authorize_download, upload_file
from app.domains.project.dependencies import get_current_member
from app.domains.project.models import ProjectMember
from app.infrastructure.database.engine import get_session
from app.infrastructure.storage.provider import StorageProvider, get_storage_provider

router = APIRouter(tags=["files"])


def _content_disposition(filename: str) -> str:
    """RFC 5987：非 ASCII 文件名走 filename*，ASCII 兜底名走 filename。"""
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
