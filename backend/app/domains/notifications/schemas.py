"""站内通知接口响应模型（12.6 节）。"""

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
    """通知列表（仅本人），附当前未读计数（2.1 节待办中心入口）。"""

    items: list[NotificationOut]
    unread_count: int
