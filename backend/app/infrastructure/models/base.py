"""ORM 基类与 Mixin。

- 主键使用 PostgreSQL UUID（pgcrypto 的 gen_random_uuid()）。
- 所有核心表包含 created_at / updated_at。
- 需要并发保护的表额外继承 VersionMixin 获得乐观锁 version 字段。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有领域模型的声明式基类。"""


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class VersionMixin:
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class CoreModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """核心表默认基类：UUID 主键 + 创建/更新时间戳。"""

    __abstract__ = True
