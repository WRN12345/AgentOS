"""T6.1 并发测试（17.2 节）：真实并发请求验证"只有一个请求生效"。

与 test_idempotency.py（串行重放）不同，这里用 asyncio.gather 让两个请求
真正并发到达（httpx ASGI transport 在同一事件循环内交错执行，各自持有
独立 DB 连接与会话）。为让交错窗口确定出现（而非依赖调度时序），
(a)/(b) 先由测试内的外部事务对目标行持 FOR UPDATE 行锁，再发起两个
并发请求——它们必然都读到旧版本并阻塞在写路径上，释放锁后才分胜负。

验证点：
(a) 重复审批：同一转派审批 / DDL 审批被两个请求同时审批时，只有一个生效，
    另一个得到 409（版本冲突或非法迁移），业务副作用（负责人/DDL 变更、
    审计事件）只发生一次；
(b) 乐观锁：两个基于同一旧版本号的 PATCH 并发到达，一个 200 一个 409
    WORK_ITEM_VERSION_CONFLICT；
(c) Idempotency-Key：同一 key 的重复创建请求并发到达，只创建一条记录、
    两个响应指向同一资源（17.2 节"重复请求返回第一次结果"的并发形态）。
"""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.audit.models import AuditEvent
from app.domains.project.models import Project, ProjectMember
from app.domains.work_items.models import WorkItem
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.models.idempotency import IdempotencyRecord
from tests.helpers_t6b import (
    create_main_deadline_change,
    create_published_item,
    create_transfer_request,
    setup_trio,
)


@contextlib.asynccontextmanager
async def _hold_row_lock(table: str, row_id: str) -> AsyncIterator[AsyncSession]:
    """外部事务对目标行持 FOR UPDATE 行锁，强制并发请求进入交错窗口。

    yield 出的会话由调用方 rollback() 释放锁；异常退出时 session.close
    同样回滚释放，不会残留锁。
    """
    async with async_session_factory() as blocker:
        await blocker.execute(
            # 表名为测试内部常量，非用户输入
            text(f"SELECT id FROM {table} WHERE id = :rid FOR UPDATE"),  # noqa: S608
            {"rid": row_id},
        )
        yield blocker


async def _audit_action_count(target_id: str, action: str) -> int:
    async with async_session_factory() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.target_id == uuid.UUID(target_id), AuditEvent.action == action)
            )
        ).scalar_one()


async def _get_work_item_row(item_id: str) -> WorkItem:
    async with async_session_factory() as session:
        row = await session.get(WorkItem, uuid.UUID(item_id))
        assert row is not None
        return row


# ---------- (a) 重复审批：并发双审批只有一个生效 ----------


async def test_concurrent_transfer_approve_only_one_takes_effect(
    client: httpx.AsyncClient, project: Project
) -> None:
    """两个并发 approve（同一版本号）：恰好一个 200、另一个 409；负责人只变更一次。"""
    ctx = await setup_trio(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    item = await create_published_item(client, ctx["leader_headers"], alice.id)  # type: ignore[arg-type]
    transfer = await create_transfer_request(
        client, ctx["alice_headers"], item["id"], bob.id  # type: ignore[arg-type]
    )
    url = f"/api/v1/transfer-requests/{transfer['id']}/approve"

    async with _hold_row_lock("transfer_requests", transfer["id"]) as blocker:
        t1 = asyncio.create_task(
            client.post(url, json={"version": 1}, headers=ctx["leader_headers"])  # type: ignore[arg-type]
        )
        t2 = asyncio.create_task(
            client.post(url, json={"version": 1}, headers=ctx["leader_headers"])  # type: ignore[arg-type]
        )
        await asyncio.sleep(0.5)  # 等两个请求都读到 v1 并阻塞在行锁上
        await blocker.rollback()  # 释放锁，两个请求竞速提交
        r1, r2 = await asyncio.gather(t1, t2)

    codes = sorted([r1.status_code, r2.status_code])
    assert codes == [200, 409], f"并发双审批应恰好一个生效: {r1.status_code}, {r2.status_code}"
    loser = r1 if r1.status_code == 409 else r2
    assert loser.json()["code"] in (
        "TRANSFER_VERSION_CONFLICT",
        "TRANSFER_INVALID_TRANSITION",
    )

    # 业务副作用只发生一次：assignee 变为 bob、item.version 只 +1、审计只有一条
    row = await _get_work_item_row(item["id"])
    assert row.assignee_id == bob.id
    assert row.version == 3  # create v1 → publish v2 → approve v3
    assert await _audit_action_count(transfer["id"], "transfer.approved") == 1


async def test_concurrent_deadline_approve_only_one_takes_effect(
    client: httpx.AsyncClient, project: Project
) -> None:
    """两个并发 DDL 审批（同一版本号）：恰好一个 200、另一个 409；DDL 只变更一次。"""
    ctx = await setup_trio(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    item = await create_published_item(client, ctx["leader_headers"], alice.id)  # type: ignore[arg-type]
    change = await create_main_deadline_change(
        client, ctx["alice_headers"], item["id"]  # type: ignore[arg-type]
    )
    url = f"/api/v1/deadline-change-requests/{change['id']}/approve"

    async with _hold_row_lock("deadline_change_requests", change["id"]) as blocker:
        t1 = asyncio.create_task(
            client.post(url, json={"version": 1}, headers=ctx["leader_headers"])  # type: ignore[arg-type]
        )
        t2 = asyncio.create_task(
            client.post(url, json={"version": 1}, headers=ctx["leader_headers"])  # type: ignore[arg-type]
        )
        await asyncio.sleep(0.5)
        await blocker.rollback()
        r1, r2 = await asyncio.gather(t1, t2)

    codes = sorted([r1.status_code, r2.status_code])
    assert codes == [200, 409], f"并发双审批应恰好一个生效: {r1.status_code}, {r2.status_code}"
    loser = r1 if r1.status_code == 409 else r2
    assert loser.json()["code"] in (
        "DEADLINE_CHANGE_VERSION_CONFLICT",
        "DEADLINE_CHANGE_INVALID_TRANSITION",
    )

    # 业务副作用只发生一次：DDL 更新、item.version 只 +1、审计只有一条
    row = await _get_work_item_row(item["id"])
    assert row.due_at is not None and row.due_at.isoformat().startswith("2026-08-15")
    assert row.version == 3  # create v1 → publish v2 → approve v3
    assert await _audit_action_count(change["id"], "deadline_change.approved") == 1


# ---------- (b) 乐观锁：并发 PATCH 基于同一旧版本号 ----------


async def test_concurrent_patch_same_version_one_conflict(
    client: httpx.AsyncClient, project: Project
) -> None:
    """两个并发 PATCH 都携带当前版本号：一个 200，另一个 409 版本冲突。"""
    ctx = await setup_trio(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    item = await create_published_item(client, ctx["leader_headers"], alice.id)  # type: ignore[arg-type]
    url = f"/api/v1/work-items/{item['id']}"

    async with _hold_row_lock("work_items", item["id"]) as blocker:
        t1 = asyncio.create_task(
            client.patch(url, json={"version": 2, "title": "并发改标题"}, headers=ctx["leader_headers"])  # type: ignore[arg-type]
        )
        t2 = asyncio.create_task(
            client.patch(
                url,
                json={"version": 2, "priority": "high"},
                headers=ctx["leader_headers"],  # type: ignore[arg-type]
            )
        )
        await asyncio.sleep(0.5)
        await blocker.rollback()
        r1, r2 = await asyncio.gather(t1, t2)

    codes = sorted([r1.status_code, r2.status_code])
    assert codes == [200, 409], f"并发更新应恰好一个成功: {r1.status_code}, {r2.status_code}"
    loser = r1 if r1.status_code == 409 else r2
    assert loser.json()["code"] == "WORK_ITEM_VERSION_CONFLICT"

    # 版本号只推进一次
    row = await _get_work_item_row(item["id"])
    assert row.version == 3


# ---------- (c) Idempotency-Key：并发重复创建只生效一次 ----------


async def test_concurrent_same_idempotency_key_creates_one_record(
    client: httpx.AsyncClient, project: Project
) -> None:
    """同一 Idempotency-Key 的创建请求并发到达：只建一条记录，两个响应指向同一资源。"""
    ctx = await setup_trio(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    headers = {**ctx["leader_headers"], "Idempotency-Key": "idem-conc-work-item-0001"}  # type: ignore[dict-item]
    payload = {
        "title": "幂等并发工作项",
        "description": "验证并发下同键只创建一条",
        "assignee_id": str(alice.id),
        "due_at": "2026-08-01T00:00:00Z",
    }

    r1, r2 = await asyncio.gather(
        client.post("/api/v1/work-items", json=payload, headers=headers),
        client.post("/api/v1/work-items", json=payload, headers=headers),
    )

    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text
    # 两个响应指向同一资源（后到请求重放首次结果）
    assert r2.json()["id"] == r1.json()["id"]

    # 业务表与幂等表都只有一条记录
    async with async_session_factory() as session:
        item_count = (
            await session.execute(select(func.count()).select_from(WorkItem))
        ).scalar_one()
        record_count = (
            await session.execute(select(func.count()).select_from(IdempotencyRecord))
        ).scalar_one()
    assert item_count == 1, "并发同键请求只应创建一条工作项"
    assert record_count == 1
