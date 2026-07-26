"""FastAPI 应用入口：模块化单体骨架（4.1 节）。"""

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.v1.router import router as v1_router
from app.core.config import settings
from app.core.logging import setup_logging
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import engine

logger = setup_logging("backend")

app = FastAPI(title="AgentOS", version="0.1.0")
app.include_router(v1_router, prefix="/api/v1")


@app.get("/health")
async def health() -> JSONResponse:
    """健康检查：进程、PostgreSQL 连接、Redis 连接三项。"""
    checks: dict[str, str] = {"process": "ok"}

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:  # noqa: BLE001 - 健康检查需要兜底所有连接异常
        logger.warning("health check: postgres unavailable: %s", type(exc).__name__)
        checks["postgres"] = "error"

    try:
        redis_client = create_redis_client()
        await redis_client.ping()
        await redis_client.aclose()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        logger.warning("health check: redis unavailable: %s", type(exc).__name__)
        checks["redis"] = "error"

    healthy = all(value == "ok" for value in checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", "checks": checks},
    )


@app.on_event("startup")
async def log_startup() -> None:
    logger.info("backend started (env=%s)", settings.app_env)
