"""Idempotency-Key 集成测试（T2.1 验收，17.2 节）。"""

import httpx

from app.domains.identity.service import create_user
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.queue.queue import QUEUE_KEY


async def test_example_task_replayed_only_written_once(client: httpx.AsyncClient) -> None:
    """同一幂等键重复调用：队列只入队一次，第二次返回首次结果。"""
    headers = {"Idempotency-Key": "idem-task-0001"}
    redis_client = create_redis_client()
    try:
        before = await redis_client.llen(QUEUE_KEY)
        r1 = await client.post("/api/v1/tasks/example", headers=headers)
        after_first = await redis_client.llen(QUEUE_KEY)
        r2 = await client.post("/api/v1/tasks/example", headers=headers)
        after_second = await redis_client.llen(QUEUE_KEY)
    finally:
        await redis_client.aclose()

    assert r1.status_code == 200
    assert after_first == before + 1  # 第一次真实执行了一次写入
    assert after_second == after_first  # 第二次没有重复写入

    assert r2.status_code == 200
    assert r2.json() == r1.json()  # 第二次返回首次结果
    assert r2.headers.get("Idempotency-Replayed") == "true"


async def test_logout_idempotent_replay(client: httpx.AsyncClient) -> None:
    """登出携带幂等键：重复请求返回首次的 200，而不是已撤销后的 401。"""
    async with async_session_factory() as session:
        await create_user(session, "alice", "Secret123!")
        await session.commit()
    tokens = (
        await client.post(
            "/api/v1/auth/login", json={"username": "alice", "password": "Secret123!"}
        )
    ).json()

    headers = {"Idempotency-Key": "idem-logout-0001"}
    payload = {"refresh_token": tokens["refresh_token"]}
    r1 = await client.post("/api/v1/auth/logout", json=payload, headers=headers)
    assert r1.status_code == 200

    r2 = await client.post("/api/v1/auth/logout", json=payload, headers=headers)
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    assert r2.headers.get("Idempotency-Replayed") == "true"

    # 不带幂等键的第三次调用 → 401，证明第二次的 200 来自重放而非 logout 自身幂等
    r3 = await client.post("/api/v1/auth/logout", json=payload)
    assert r3.status_code == 401
    assert r3.json()["code"] == "REFRESH_TOKEN_INVALID"
