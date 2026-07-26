"""测试基础设施：独立测试库，不污染 agentos 主库。

- 在任何 app 模块导入前，把 DATABASE_URL 的库名改为 <原名>_test（如 agentos_test）、
  REDIS_URL 切到 db 15（避开运行中的 scheduler/worker 使用的 db 0）；
- 会话级前置：自动建测试库（不存在则 CREATE DATABASE）并执行 alembic upgrade head；
- 每个用例结束后清空业务表，保证用例间隔离。

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
if _rparts.path != "/15":
    os.environ["REDIS_URL"] = urlunsplit(_rparts._replace(path="/15"))

import asyncio  # noqa: E402
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
                "TRUNCATE refresh_tokens, users, audit_events, idempotency_records, "
                "member_capabilities, work_item_collaborators, work_items, "
                "project_members, projects"
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


# ---------- 项目/成员测试辅助（T2.3 起） ----------


@pytest.fixture
async def project() -> Project:
    """创建测试用默认项目（每个用例结束后随 TRUNCATE 清理）。"""
    async with async_session_factory() as session:
        p = Project(name="测试项目", description="测试用")
        session.add(p)
        await session.commit()
        return p


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


@pytest.fixture
async def leader(project: Project) -> ProjectMember:
    """项目负责人成员身份（账号 leader / Leader123!）。"""
    _, member = await add_member(project, "leader", "Leader123!", role="leader", display_name="负责人")
    return member


async def auth_headers(client: httpx.AsyncClient, username: str, password: str) -> dict[str, str]:
    """登录并返回 Authorization 头。"""
    resp = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}
