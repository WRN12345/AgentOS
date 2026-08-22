"""文件接口响应模型（12.5 节）。

不暴露 storage_key：下载一律走 GET /files/{id}/download，
客户端无需也不应感知存储内部键（16 节最小暴露）。
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
    # 版本链（设计文档第 3 节）：同名上传递增；superseded_by 非空表示已被新版本取代
    version: int
    superseded_by: uuid.UUID | None
    # 索引状态（设计文档第 6 节）：pending/indexing/indexed/failed/unindexed
    index_status: str
    created_at: datetime
    updated_at: datetime
