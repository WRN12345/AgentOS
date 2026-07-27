"""Alembic 环境：异步引擎，连接串来自 app.core.config（统一配置）。"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import settings

# 导入全部领域模型，确保 Base.metadata 完整（供 alembic revision --autogenerate 识别）
from app.domains.audit import models as _audit_models  # noqa: F401
from app.domains.collaboration import models as _collaboration_models  # noqa: F401
from app.domains.deadlines import models as _deadline_models  # noqa: F401
from app.domains.deliverables import models as _deliverable_models  # noqa: F401
from app.domains.files import models as _file_models  # noqa: F401
from app.domains.identity import models as _identity_models  # noqa: F401
from app.domains.notifications import models as _notification_models  # noqa: F401
from app.domains.project import models as _project_models  # noqa: F401
from app.domains.reviews import models as _review_models  # noqa: F401
from app.domains.transfers import models as _transfer_models  # noqa: F401
from app.domains.work_items import models as _work_item_models  # noqa: F401
from app.infrastructure.models import idempotency as _idempotency_model  # noqa: F401
from app.infrastructure.models.base import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
