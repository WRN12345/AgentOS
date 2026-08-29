"""协作请求接口的请求与响应模型。

状态命令均携带 `version` 实施乐观锁；版本不匹配返回 `409` 和
`COLLABORATION_VERSION_CONFLICT`，成功后版本递增。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.domains.work_items.schemas import MemberBrief


class CollaborationRequestCreateIn(BaseModel):
    """由工作项当前主执行人直接发起协作请求。"""

    assignee_id: uuid.UUID  # 接收人必须是同项目的其他活跃成员。
    title: str = Field(min_length=1, max_length=255)
    goal: str = Field(min_length=1)
    template: str | None = None
    due_at: datetime | None = None


class CollaborationCommandIn(BaseModel):
    """携带乐观锁版本号的状态命令。"""

    version: int = Field(ge=1)


class CollaborationSubmitIn(BaseModel):
    """回传文本产物，并可附带本工作项的交付物或文件引用。"""

    version: int = Field(ge=1)
    result_text: str = Field(min_length=1)
    deliverable_id: uuid.UUID | None = None  # 可选的本工作项交付物版本。
    file_id: uuid.UUID | None = None  # 可选的已上传文件。


class CollaborationRevisionIn(BaseModel):
    """要求修改；`feedback` 仅写入审计摘要，不进入通知正文。"""

    version: int = Field(ge=1)
    feedback: str | None = None


class CollaborationRequestOut(BaseModel):
    id: uuid.UUID
    work_item_id: uuid.UUID
    work_item_title: str
    requester: MemberBrief
    assignee: MemberBrief
    title: str
    goal: str
    template: str | None
    due_at: datetime | None
    result_text: str | None
    result_deliverable_id: uuid.UUID | None
    result_file_id: uuid.UUID | None
    status: str
    version: int
    created_at: datetime
    updated_at: datetime


class CollaborationRequestSummaryOut(BaseModel):
    """不包含 `goal`、`template` 和 `result_text` 正文的协作摘要。"""

    id: uuid.UUID
    work_item_id: uuid.UUID
    work_item_title: str
    requester: MemberBrief
    assignee: MemberBrief
    title: str
    status: str
    due_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime
