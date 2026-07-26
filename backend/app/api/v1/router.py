"""API v1 占位路由。

阶段 1 只提供骨架路由，业务接口在阶段 2 起按领域落地。
"""

from typing import Annotated

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from app.core.logging import setup_logging
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.queue.queue import enqueue

logger = setup_logging("backend")

router = APIRouter()


@router.get("/")
async def v1_root() -> dict[str, str]:
    return {"service": "agentos", "api": "v1", "status": "ok"}


@router.post("/auth/login")
async def login_placeholder(
    payload: Annotated[dict, Body()],
) -> JSONResponse:
    """登录占位路由（T2.1 落地真实认证）。

    日志只记录用户名，绝不记录密码或令牌原文（第 16 章）。
    """
    username = str(payload.get("username", "<unknown>"))
    logger.info("login attempt: username=%s", username)
    return JSONResponse(
        status_code=501,
        content={
            "code": "NOT_IMPLEMENTED",
            "message": "认证将在阶段 2 提供",
            "request_id": "",
            "details": {},
        },
    )


@router.post("/tasks/example")
async def enqueue_example_task() -> dict[str, str]:
    """投递一个示例后台任务，用于验证 API → 队列 → Worker 链路。"""
    redis_client = create_redis_client()
    try:
        task = await enqueue(redis_client, "example.ping", {"source": "api"})
    finally:
        await redis_client.aclose()
    logger.info("enqueued example task: id=%s", task["id"])
    return {"task_id": task["id"], "type": task["type"]}
