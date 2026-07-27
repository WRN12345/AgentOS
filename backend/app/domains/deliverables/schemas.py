"""交付物接口请求/响应模型（7.5、12.5 节）。

- git_link / text 类型必须携带 content；file 类型必须携带 file_id（引用已上传文件）；
- file 类型响应内嵌文件摘要（含 sha256），可追溯 stored_files 哈希记录（2.1 节）。
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.domains.work_items.schemas import MemberBrief


class DeliverableCreateIn(BaseModel):
    """提交新版本交付物：版本号由服务端按工作项递增（7.5 节），客户端不传。"""

    type: Literal["git_link", "text", "file"]
    content: str | None = Field(default=None, min_length=1)
    file_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _check_type_payload(self) -> "DeliverableCreateIn":
        if self.type in ("git_link", "text"):
            if not self.content:
                raise ValueError("git_link/text 类型必须携带 content")
            if self.file_id is not None:
                raise ValueError("git_link/text 类型不应携带 file_id")
        else:  # file
            if self.file_id is None:
                raise ValueError("file 类型必须携带 file_id")
            if self.content:
                raise ValueError("file 类型不应携带 content")
        return self


class FileBrief(BaseModel):
    """file 类型交付物引用的文件摘要：sha256 供完整性追溯。"""

    id: uuid.UUID
    original_filename: str
    size_bytes: int
    mime_type: str
    sha256: str


class DeliverableOut(BaseModel):
    id: uuid.UUID
    work_item_id: uuid.UUID
    type: str
    content: str | None
    file: FileBrief | None
    version: int
    submitted_by: MemberBrief
    created_at: datetime
    updated_at: datetime
