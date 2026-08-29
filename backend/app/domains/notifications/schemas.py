"""站内通知接口响应模型。"""

import uuid
from datetime import datetime

from pydantic import BaseModel


class NotificationOut(BaseModel):
    id: uuid.UUID
    type: str
    title: str
    body: str
    link: str | None
    is_read: bool
    read_at: datetime | None
    created_at: datetime


class NotificationListOut(BaseModel):
    """当前用户的通知列表及未读总数。"""

    items: list[NotificationOut]
    unread_count: int
