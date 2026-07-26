"""API v1 路由组装。

领域接口按 4.1 节放进对应领域包，这里只做挂载；
501 登录占位已在 T2.2 由 domains/identity 的真实认证替代。
"""

from fastapi import APIRouter, Depends

from app.core.idempotency import idempotency_guard
from app.core.logging import setup_logging
from app.domains.audit.router import router as audit_router
from app.domains.identity.router import router as auth_router
from app.domains.project.router import router as members_router
from app.domains.work_items.router import router as work_items_router
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.queue.queue import enqueue

logger = setup_logging("backend")

router = APIRouter()

router.include_router(auth_router)
router.include_router(audit_router)
router.include_router(members_router)
router.include_router(work_items_router)


@router.get("/")
async def v1_root() -> dict[str, str]:
    return {"service": "agentos", "api": "v1", "status": "ok"}


@router.post("/tasks/example")
async def enqueue_example_task(_: None = Depends(idempotency_guard)) -> dict[str, str]:
    """投递一个示例后台任务，用于验证 API → 队列 → Worker 链路。

    同时作为 Idempotency-Key 机制的示例命令接口：
    携带同一幂等键的重复请求只入队一次，第二次返回首次结果。
    """
    redis_client = create_redis_client()
    try:
        task = await enqueue(redis_client, "example.ping", {"source": "api"})
    finally:
        await redis_client.aclose()
    logger.info("enqueued example task: id=%s", task["id"])
    return {"task_id": task["id"], "type": task["type"]}
