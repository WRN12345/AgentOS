"""Idempotency-Key 集成测试，覆盖串行重放、错误恢复和项目隔离。"""

import httpx
from sqlalchemy import select

from app.domains.identity.service import create_user
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.models.idempotency import IdempotencyRecord
from app.infrastructure.queue.queue import QUEUE_KEY
from tests.conftest import add_member, add_member_for_existing_user, auth_headers


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
    assert after_first == before + 1
    assert after_second == after_first

    assert r2.status_code == 200
    assert r2.json() == r1.json()
    assert r2.headers.get("Idempotency-Replayed") == "true"


async def test_logout_idempotent_replay(client: httpx.AsyncClient) -> None:
    """登出本身为幂等撤销，不依赖 user_id=NULL 的通用幂等桶。"""
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
    assert r2.headers.get("Idempotency-Replayed") is None

    # 固定返回 200，避免泄漏 refresh token 是否曾存在。
    r3 = await client.post("/api/v1/auth/logout", json=payload)
    assert r3.status_code == 200


async def test_validation_error_is_not_cached(client: httpx.AsyncClient, project_a) -> None:
    """同键首次 422 后修正参数应真实执行，不能永久重放错误响应。"""
    _, leader = await add_member(
        project_a, "idem_leader", "Leader123!", role="leader", display_name="负责人"
    )
    _, alice = await add_member(project_a, "idem_alice", "Alice123!", display_name="爱丽丝")
    headers = await auth_headers(
        client, "idem_leader", "Leader123!", project_id=str(project_a.id)
    )
    headers["Idempotency-Key"] = "idem-fix-validation-0001"

    invalid = await client.post("/api/v1/work-items", json={"title": "缺负责人"}, headers=headers)
    assert invalid.status_code == 422
    fixed = await client.post(
        "/api/v1/work-items",
        json={"title": "参数已修正", "assignee_id": str(alice.id)},
        headers=headers,
    )
    assert fixed.status_code == 201, fixed.text
    assert fixed.headers.get("Idempotency-Replayed") is None


async def test_idempotency_key_scoped_per_project(
    client: httpx.AsyncClient, project_a, project_b
) -> None:
    """同一幂等键在不同项目中独立执行和复用，在同一项目中重放首次结果。"""
    async with async_session_factory() as session:
        leader = await create_user(session, "leader", "Leader123!")
        await session.commit()
    await add_member_for_existing_user(
        async_session_factory, project_a, leader, role="leader", display_name="负责人"
    )
    await add_member_for_existing_user(
        async_session_factory, project_b, leader, role="leader", display_name="负责人"
    )
    _, alice = await add_member(project_a, "alice", "Alice123!", display_name="爱丽丝")
    _, bob = await add_member(project_b, "bob", "Bob123!", display_name="鲍勃")

    headers_a = await auth_headers(client, "leader", "Leader123!", project_id=str(project_a.id))
    headers_b = await auth_headers(client, "leader", "Leader123!", project_id=str(project_b.id))

    key = "idem-cross-project-0001"
    payload_a = {"title": "项目A幂等", "assignee_id": str(alice.id)}
    payload_b = {"title": "项目B幂等", "assignee_id": str(bob.id)}

    r_a1 = await client.post(
        "/api/v1/work-items", json=payload_a, headers={**headers_a, "Idempotency-Key": key}
    )
    assert r_a1.status_code == 201
    assert r_a1.headers.get("Idempotency-Replayed") is None

    r_b1 = await client.post(
        "/api/v1/work-items", json=payload_b, headers={**headers_b, "Idempotency-Key": key}
    )
    assert r_b1.status_code == 201
    assert r_b1.json()["id"] != r_a1.json()["id"]

    r_a2 = await client.post(
        "/api/v1/work-items", json=payload_a, headers={**headers_a, "Idempotency-Key": key}
    )
    assert r_a2.status_code == 201
    assert r_a2.json()["id"] == r_a1.json()["id"]
    assert r_a2.headers.get("Idempotency-Replayed") == "true"

    r_b2 = await client.post(
        "/api/v1/work-items", json=payload_b, headers={**headers_b, "Idempotency-Key": key}
    )
    assert r_b2.status_code == 201
    assert r_b2.json()["id"] == r_b1.json()["id"]
    assert r_b2.headers.get("Idempotency-Replayed") == "true"

    async with async_session_factory() as session:
        records = (
            await session.execute(
                select(IdempotencyRecord).where(IdempotencyRecord.key == key)
            )
        ).scalars().all()
    assert len(records) == 2
    assert {r.project_id for r in records} == {project_a.id, project_b.id}
