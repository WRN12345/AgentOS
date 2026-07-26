"""审计查询接口模型。"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class AuditEventOut(BaseModel):
    id: uuid.UUID
    actor_id: uuid.UUID | None
    action: str
    target_type: str | None
    target_id: uuid.UUID | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    request_id: str | None
    source_ip: str | None
    created_at: datetime
