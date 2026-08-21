"""测试基础设施：独立测试库，不污染 agentos 主库。

- 在任何 app 模块导入前，把 DATABASE_URL 的库名改为 <原名>_test（如 agentos_test）、
  REDIS_URL 切到 db 15（避开运行中的 scheduler/worker 使用的 db 0）；
- 会话级前置：自动建测试库（不存在则 CREATE DATABASE）并执行 alembic upgrade head；
- 每个用例结束后清空业务表，保证用例间隔离。
- 多项目支持：project_a/project_b 双项目 fixture、admin 全局角色 helper。

运行方式（容器内，postgres/redis 在 Compose 网络内可达）：
    docker compose exec backend pytest
"""

import os
import subprocess
from urllib.parse import urlsplit, urlunsplit

# --- 环境改写必须发生在任何 app 模块导入之前 ---
_db_url = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://agentos:agentos-dev-password@postgres:5432/agentos"
)
_parts = urlsplit(_db_url)
if not _parts.path.endswith("_test"):
    os.environ["DATABASE_URL"] = urlunsplit(_parts._replace(path=f"{_parts.path}_test"))

_redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
_rparts = urlsplit(_redis_url)
# 默认切到 db 15；显式指定了非 0 库（如并行跑多套测试时的 db 13/14）则尊重环境变量
if _rparts.path in ("", "/0"):
    os.environ["REDIS_URL"] = urlunsplit(_rparts._replace(path="/15"))

import asyncio  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import asyncpg  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.domains.identity.models import User  # noqa: E402
from app.domains.identity.service import create_user  # noqa: E402
from app.domains.project.models import Project, ProjectMember  # noqa: E402
from app.infrastructure.database.engine import async_session_factory, engine  # noqa: E402
from app.main import app  # noqa: E402

# 本地 Windows：psycopg 异步连接不能用 ProactorEventLoop，需切 SelectorEventLoop。
# 仅 win32 生效；Docker/Linux 下事件循环策略本来就合适，此处为 no-op。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _test_db_name() -> str:
    return urlsplit(settings.database_url).path.lstrip("/")


async def _ensure_test_database() -> None:
    """连接维护库 postgres，测试库不存在则创建（agentos 为容器超级用户）。"""
    admin_url = urlsplit(settings.database_url.replace("+asyncpg", ""))
    admin_dsn = urlunsplit(admin_url._replace(path="/postgres"))
    conn = await asyncpg.connect(admin_dsn)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", _test_db_name()
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{_test_db_name()}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session", autouse=True)
def _prepare_test_database() -> None:
    asyncio.run(_ensure_test_database())
    subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=os.environ,
        check=True,
        capture_output=True,
    )


@pytest.fixture(autouse=True)
async def _clean_tables() -> None:
    yield
    async with async_session_factory() as session:
        await session.execute(
            text(
                "TRUNCATE reviews, deliverables, refresh_tokens, users, audit_events, "
                "idempotency_records, "
                "member_capabilities, work_item_collaborators, work_items, "
                "collaboration_requests, notifications, "
                "transfer_requests, deadline_change_requests, stored_files, "
                "agent_suggestions, agent_runs, dev_docs, "
                "memory_chunks, project_members, projects"
            )
        )
        # LangGraph 检查点表由 worker 首次运行时才创建（不归 Alembic 管理），
        # 存在才清空，保证用例间隔离
        await session.execute(
            text(
                "DO $$ BEGIN "
                "IF to_regclass('checkpoints') IS NOT NULL THEN "
                "TRUNCATE checkpoints, checkpoint_blobs, checkpoint_writes; "
                "END IF; END $$"
            )
        )
        await session.commit()
    # pytest-asyncio 每个用例一个新事件循环；释放连接池，避免连接跨循环复用
    await engine.dispose()


@pytest.fixture
async def client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------- 项目/成员测试辅助 ----------


@pytest.fixture
async def project_a() -> Project:
    """创建测试用项目 A（默认项目）。"""
    async with async_session_factory() as session:
        p = Project(name="项目 A", description="测试项目 A")
        session.add(p)
        await session.commit()
        return p


@pytest.fixture
async def project_b() -> Project:
    """创建测试用项目 B。"""
    async with async_session_factory() as session:
        p = Project(name="项目 B", description="测试项目 B")
        session.add(p)
        await session.commit()
        return p


# 向后兼容：project 别名指向 project_a
@pytest.fixture
async def project(project_a: Project) -> Project:
    """向后兼容别名：默认项目 = 项目 A。"""
    return project_a


async def add_member(
    project: Project,
    username: str,
    password: str,
    *,
    role: str = "member",
    display_name: str | None = None,
) -> tuple[User, ProjectMember]:
    """直接建库创建成员账号 + 项目成员身份（绕过 API，供测试准备数据）。"""
    async with async_session_factory() as session:
        user = await create_user(session, username, password)
        member = ProjectMember(
            project_id=project.id,
            user_id=user.id,
            role=role,
            display_name=display_name or username,
        )
        member.capabilities = []
        session.add(member)
        await session.commit()
        return user, member


async def add_member_for_existing_user(
    session_factory,
    project: Project,
    user: User,
    *,
    role: str = "member",
    display_name: str | None = None,
) -> ProjectMember:
    """为已有用户在另一项目中创建成员身份（跨项目测试用，不重复建 User）。"""
    async with session_factory() as session:
        member = ProjectMember(
            project_id=project.id,
            user_id=user.id,
            role=role,
            display_name=display_name or user.username,
        )
        member.capabilities = []
        session.add(member)
        await session.commit()
        return member


async def create_admin_user(
    username: str = "admin",
    password: str = "Admin123!",
) -> User:
    """创建全局管理员（is_admin=True，无项目成员记录）。"""
    async with async_session_factory() as session:
        user = await create_user(session, username, password)
        user.is_admin = True
        await session.commit()
        return user


@pytest.fixture
async def leader(project_a: Project) -> ProjectMember:
    """项目 A 负责人成员身份（账号 leader / Leader123!）。"""
    _, member = await add_member(
        project_a, "leader", "Leader123!", role="leader", display_name="负责人"
    )
    return member


@pytest.fixture
async def admin_user() -> User:
    """全局管理员（is_admin=True，不属于任何项目）。"""
    return await create_admin_user("admin", "Admin123!")


async def auth_headers(
    client: httpx.AsyncClient,
    username: str,
    password: str,
    *,
    project_id: str | None = None,
) -> dict[str, str]:
    """登录并返回 Authorization + 可选的 X-Project-Id 头。

    project_id: 若提供，同时携带 X-Project-Id（用于多项目测试）。
    """
    resp = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    if project_id:
        headers["X-Project-Id"] = project_id
    return headers


# ---------- 便捷 fixture：常用角色 header ----------


@pytest.fixture
async def leader_headers_a(client: httpx.AsyncClient, project_a: Project) -> dict[str, str]:
    """项目 A 负责人 header（Authorization + X-Project-Id）。"""
    return await auth_headers(client, "leader", "Leader123!", project_id=str(project_a.id))


@pytest.fixture
async def leader_headers_b(client: httpx.AsyncClient, project_b: Project) -> dict[str, str]:
    """项目 B 负责人 header（Authorization + X-Project-Id）。"""
    return await auth_headers(client, "leader", "Leader123!", project_id=str(project_b.id))


@pytest.fixture
async def admin_headers(client: httpx.AsyncClient, admin_user: User) -> dict[str, str]:
    """全局管理员 header（仅 Authorization，不带项目上下文）。"""
    return await auth_headers(client, "admin", "Admin123!")
