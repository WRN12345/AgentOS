"""文件接口响应模型（12.5 节）。

不暴露 storage_key：下载一律走 GET /files/{id}/download，
客户端无需也不应感知存储内部键（16 节最小暴露）。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel


class StoredFileOut(BaseModel):
    id: uuid.UUID
    original_filename: str
    size_bytes: int
    mime_type: str
    sha256: str
    storage_backend: str
    uploaded_by: uuid.UUID
    work_item_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
