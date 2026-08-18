"""幂等记录表（17.2 节）：命令接口首次响应的持久化。

唯一性（project + user + key + endpoint）由迁移 0018 中的表达式唯一索引保证：
COALESCE(project_id, 零值 UUID) + COALESCE(user_id, 零值 UUID) + key + method + path，
跨项目、跨用户均不串用（多项目后同一键在 A/B 项目下视为不同请求）。
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
    # 项目归属：从 X-Project-Id 头快照捕获（守卫读取）；无项目上下文的全局接口记为 NULL
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(255), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
