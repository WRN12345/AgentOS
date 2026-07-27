"""协作请求接口请求/响应模型（12.4 节）。

命令接口（accept/decline/start/submit/request-revision/complete/cancel）
均要求携带 version 做乐观锁（17.2 节）：不匹配返回 409
COLLABORATION_VERSION_CONFLICT，成功后 version + 1。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.domains.work_items.schemas import MemberBrief


class CollaborationRequestCreateIn(BaseModel):
    """发起协作请求：发起人必须是工作项当前主执行人（7.2 节，无需负责人审批）。"""

    assignee_id: uuid.UUID  # 接收人：不能是自己，必须是项目活跃成员
    title: str = Field(min_length=1, max_length=255)
    goal: str = Field(min_length=1)
    template: str | None = None
    due_at: datetime | None = None


class CollaborationCommandIn(BaseModel):
    """状态命令（accept/decline/start/complete/cancel）：携带乐观锁版本号。"""

    version: int = Field(ge=1)


class CollaborationSubmitIn(BaseModel):
    """回传产物（submit）：result_text 为文本产物；T4.4 起可附带交付物/文件引用。"""

    version: int = Field(ge=1)
    result_text: str = Field(min_length=1)
    deliverable_id: uuid.UUID | None = None  # 引用本工作项的交付物版本（可选）
    file_id: uuid.UUID | None = None  # 引用已上传文件（可选）


class CollaborationRevisionIn(BaseModel):
    """要求修改（request-revision）：feedback 记入审计 after 摘要，不进通知正文（16 节）。"""

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
    """摘要列表（13.2 节"我的协作"）：不含 goal/template/result_text 正文。"""

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
