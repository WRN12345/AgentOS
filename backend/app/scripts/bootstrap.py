"""初始数据引导（幂等）：默认项目 + 管理员账号与成员身份。

首版不开放公开注册（16 节），新环境通过本脚本获得首个可登录账号，
并把它登记为默认项目（2.2 节单项目）的 admin 成员（管理员：查看全部 +
成员账号管理；负责人由管理员登录后通过成员管理创建）。
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
from app.domains.project.models import ROLE_ADMIN, Project, ProjectMember
from app.infrastructure.database.engine import async_session_factory, engine

logger = setup_logging("backend")


async def ensure_default_project() -> Project:
    """幂等创建默认项目（首版只有一条有效项目记录）。"""
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
    """幂等创建初始管理员账号：已存在则直接返回。"""
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
            await session.commit()
        return user


async def ensure_admin_membership(project: Project, user: User) -> bool:
    """幂等把初始账号登记为默认项目的 admin 成员。返回是否新建。"""
    async with async_session_factory() as session:
        existing = (
            await session.execute(
                select(ProjectMember).where(
                    ProjectMember.project_id == project.id,
                    ProjectMember.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return False
        session.add(
            ProjectMember(
                project_id=project.id,
                user_id=user.id,
                role=ROLE_ADMIN,
                display_name=settings.bootstrap_admin_display_name,
            )
        )
        await session.commit()
        return True


async def main() -> None:
    project = await ensure_default_project()
    user = await ensure_admin()
    membership_created = await ensure_admin_membership(project, user)
    logger.info(
        "bootstrap: project=%s, admin=%s, admin membership %s",
        settings.bootstrap_project_name,
        settings.bootstrap_admin_username,
        "created" if membership_created else "already exists",
    )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
