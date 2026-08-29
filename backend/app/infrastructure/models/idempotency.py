"""持久化命令接口的首次响应，用于幂等重放。

表达式唯一索引以 `COALESCE(project_id, 零值 UUID)`、`COALESCE(user_id, 零值 UUID)`、
`key`、`method` 和 `path` 保证唯一性，使同一幂等键可在不同项目或用户下独立使用。
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.models.base import Base, UUIDPrimaryKeyMixin


class IdempotencyRecord(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "idempotency_records"

    key: Mapped[str] = mapped_column(String(128), nullable=False)
    # 项目归属取自 `X-Project-Id` 快照；全局接口没有项目上下文，记为 NULL。
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(255), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
