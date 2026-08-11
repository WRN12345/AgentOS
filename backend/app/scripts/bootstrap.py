"""初始数据引导（幂等）：默认项目 + 全局管理员账号。

管理员升级为全局角色（users.is_admin），不属于任何项目、
不参与业务协作，通过管理控制台管理平台。
用法：python -m app.scripts.bootstrap（backend 容器启动时在迁移后自动执行）。
配置：BOOTSTRAP_ADMIN_USERNAME / BOOTSTRAP_ADMIN_PASSWORD /
BOOTSTRAP_PROJECT_NAME / BOOTSTRAP_ADMIN_DISPLAY_NAME。
日志只记录用户名与项目名，绝不记录密码。
"""

import asyncio

from sqlalchemy import select

from app.core.config import settings
from app.core.logging import setup_logging
from app.domains.identity.models import User
from app.domains.identity.service import create_user
from app.domains.project.models import Project
from app.infrastructure.database.engine import async_session_factory, engine

logger = setup_logging("backend")


async def ensure_default_project() -> Project:
    """幂等创建默认项目。"""
    async with async_session_factory() as session:
        project = (
            await session.execute(
                select(Project).where(Project.name == settings.bootstrap_project_name)
            )
        ).scalar_one_or_none()
        if project is None:
            project = Project(name=settings.bootstrap_project_name)
            session.add(project)
            await session.commit()
        return project


async def ensure_admin() -> User:
    """幂等创建全局管理员账号（users.is_admin = True，不创建项目成员记录）。"""
    async with async_session_factory() as session:
        user = (
            await session.execute(
                select(User).where(User.username == settings.bootstrap_admin_username)
            )
        ).scalar_one_or_none()
        if user is None:
            user = await create_user(
                session, settings.bootstrap_admin_username, settings.bootstrap_admin_password
            )
            user.is_admin = True
            await session.commit()
        elif not user.is_admin:
            # 已存在的初始账号但尚未标记为 admin（向前兼容旧部署）
            user.is_admin = True
            await session.commit()
        return user


async def main() -> None:
    project = await ensure_default_project()
    admin_user = await ensure_admin()
    logger.info(
        "bootstrap: project=%s, global admin=%s (is_admin=%s)",
        settings.bootstrap_project_name,
        settings.bootstrap_admin_username,
        admin_user.is_admin,
    )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
