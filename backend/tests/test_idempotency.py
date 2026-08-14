"""Idempotency-Key 集成测试（T2.1 验收，17.2 节）。

含 ticket 06（幂等键并入项目维度）的跨项目验收：同一键在 A/B 项目下
视为不同请求，各自独立执行、各自复用；同项目内同键仍复用。
"""

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


async def test_idempotency_key_scoped_per_project(
    client: httpx.AsyncClient, project_a, project_b
) -> None:
    """ticket 06 验收：同一键在 A/B 不同项目下视为不同请求，各自独立执行与复用。

    用工作项创建接口（项目域幂等写接口）做载体：leader 在 A/B 双项目均为负责人。
    """
    # leader 账号建一次，再复用同一账号分别挂到 A/B 两项目（避免重复建 User）
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

    # 项目 A 首次：真实执行
    r_a1 = await client.post(
        "/api/v1/work-items", json=payload_a, headers={**headers_a, "Idempotency-Key": key}
    )
    assert r_a1.status_code == 201
    assert r_a1.headers.get("Idempotency-Replayed") is None

    # 项目 B 同键：不复用 A 的响应，独立执行出独立工作项
    r_b1 = await client.post(
        "/api/v1/work-items", json=payload_b, headers={**headers_b, "Idempotency-Key": key}
    )
    assert r_b1.status_code == 201
    assert r_b1.json()["id"] != r_a1.json()["id"]

    # 同项目内同键仍复用（A、B 各自独立重放）
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

    # 数据库层：同一键只落两条记录，各属一个项目（唯一索引按项目维度生效）
    async with async_session_factory() as session:
        records = (
            await session.execute(
                select(IdempotencyRecord).where(IdempotencyRecord.key == key)
            )
        ).scalars().all()
    assert len(records) == 2
    assert {r.project_id for r in records} == {project_a.id, project_b.id}
