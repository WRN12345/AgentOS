"""SQLAlchemy 2 异步引擎与会话管理（第 11 章）。"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

# hide_parameters：SQL 语句参数可能包含问答内容、档案等私人数据，
# 避免异常/日志输出时随语句文本泄露
engine = create_async_engine(
    settings.database_url, pool_pre_ping=True, hide_parameters=True
)

async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：提供一次请求范围内的异步会话。"""
    async with async_session_factory() as session:
        yield session
