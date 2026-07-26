"""FastAPI 应用入口：模块化单体骨架（4.1 节）。"""

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.router import router as v1_router
from app.core.config import settings
from app.core.errors import ApiException, ErrorCodes, IdempotentReplay
from app.core.idempotency import IdempotencyMiddleware
from app.core.logging import setup_logging
from app.core.middleware import RequestContextMiddleware
from app.core.request_context import get_request_id
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import engine

logger = setup_logging("backend")

app = FastAPI(title="AgentOS", version="0.1.0")

# 中间件（后添加的在更内层）：request_id 上下文在最外层，幂等持久化在其内
app.add_middleware(IdempotencyMiddleware)
app.add_middleware(RequestContextMiddleware)


def _error_body(code: str, message: str, details: dict | None = None) -> dict:
    """统一错误格式（17.1 节）。"""
    return {
        "code": code,
        "message": message,
        "request_id": get_request_id(),
        "details": details or {},
    }


@app.exception_handler(ApiException)
async def api_exception_handler(request: Request, exc: ApiException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(exc.code, exc.message, exc.details),
    )


@app.exception_handler(IdempotentReplay)
async def idempotent_replay_handler(request: Request, exc: IdempotentReplay) -> JSONResponse:
    """幂等命中：直接返回首次响应。"""
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.body,
        headers={"Idempotency-Replayed": "true"},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=_error_body(
            ErrorCodes.VALIDATION_ERROR,
            "请求参数校验失败",
            {"errors": jsonable_encoder(exc.errors())},
        ),
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = {
        404: ErrorCodes.NOT_FOUND,
        405: ErrorCodes.METHOD_NOT_ALLOWED,
    }.get(exc.status_code, f"HTTP_{exc.status_code}")
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(code, str(exc.detail)),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # 日志纪律（16 章）：异常只记类型名，避免连接串等敏感信息落日志
    logger.error("unhandled error: %s (request_id=%s)", type(exc).__name__, get_request_id())
    return JSONResponse(
        status_code=500,
        content=_error_body(ErrorCodes.INTERNAL_ERROR, "服务器内部错误"),
    )


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
