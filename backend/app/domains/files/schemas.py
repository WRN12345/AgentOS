"""文件接口响应模型。

不暴露 `storage_key`：下载一律走 `GET /files/{id}/download`，
客户端无需也不应感知存储内部键，避免暴露存储实现细节。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel


class StoredFileOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    original_filename: str
    size_bytes: int
    mime_type: str
    sha256: str
    storage_backend: str
    uploaded_by: uuid.UUID
    work_item_id: uuid.UUID | None
    # 同名上传递增版本；`superseded_by` 非空表示已被新版本取代。
    version: int
    superseded_by: uuid.UUID | None
    index_status: str
    created_at: datetime
    updated_at: datetime
