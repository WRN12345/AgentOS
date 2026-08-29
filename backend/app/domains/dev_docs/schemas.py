"""开发文档接口的请求与响应模型。

写接口携带 `version` 实施乐观锁；版本不匹配返回 `409 DEV_DOC_VERSION_CONFLICT`，
成功后版本递增。`PUT` 使用 `upsert` 语义：新建时省略 `version`，更新时必须携带。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.domains.work_items.schemas import MemberBrief


class DevDocUpdateIn(BaseModel):
    """撰写/编辑草稿：仅主执行人，DRAFT/RETURNED 状态可编辑。

    `version` 为 `None` 表示创建新文档；文档已存在时必须提供以实施乐观锁。
    """

    content: str = Field(default="", max_length=100_000)
    version: int | None = Field(default=None, ge=1)


class DevDocCommandIn(BaseModel):
    """携带乐观锁版本号的状态命令。"""

    version: int = Field(ge=1)


class DevDocReturnIn(BaseModel):
    """打回命令：必须附理由（退回给撰写人修改）。"""

    version: int = Field(ge=1)
    review_note: str = Field(min_length=1)


class DevDocWaiveIn(BaseModel):
    """豁免命令：文档不存在时创建占位行（version 省略）；已存在时必带乐观锁版本号。"""

    version: int | None = Field(default=None, ge=1)


class DevDocOut(BaseModel):
    id: uuid.UUID
    work_item_id: uuid.UUID
    work_item_title: str
    author: MemberBrief | None
    content: str
    status: str
    review_note: str | None
    confirmed_by: MemberBrief | None
    confirmed_at: datetime | None
    doc_version: int
    waived: bool
    # LLM 不可用时允许缺少最近一次 Agent 初审建议。
    latest_review_suggestion_id: uuid.UUID | None
    version: int
    created_at: datetime
    updated_at: datetime
