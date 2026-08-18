"""实时事件通道与 SSE 端点集成测试（T3.6 验收，4.3、12.6、16 节）。

覆盖：
- 业务动作 commit 后，对应成员频道能收到 Redis Pub/Sub 事件（载荷结构完整）；
- 工作项状态变化 → 对端成员收到 work_item.* 事件；
- 协作/转派/DDL 通知 → 同内容事件；
- GET /events/stream 认证（无 token/假 token → 401）与端到端流式下发；
- 他人频道收不到本成员事件（16 节最小暴露）。
"""

import asyncio
import json
import uuid
from contextlib import suppress

import httpx
import pytest
import redis.asyncio as redis

from app.domains.project.models import Project, ProjectMember
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.events import channel_for
from app.main import app
from tests.conftest import add_member, auth_headers

LEADER_PW = "Leader123!"
ALICE_PW = "Alice123!"
BOB_PW = "Bob123!"


async def _setup(client: httpx.AsyncClient, project: Project) -> dict[str, object]:
    """leader + alice（主执行人）+ bob，并建好一个 DRAFT 工作项（未发布）。"""
    _, leader = await add_member(project, "leader", LEADER_PW, role="leader", display_name="负责人")
    _, alice = await add_member(project, "alice", ALICE_PW, display_name="爱丽丝")
    _, bob = await add_member(project, "bob", BOB_PW, display_name="鲍勃")
    ctx: dict[str, object] = {
        "leader": leader,
        "alice": alice,
        "bob": bob,
        "leader_headers": await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id)),
        "alice_headers": await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id)),
        "bob_headers": await auth_headers(client, "bob", BOB_PW, project_id=str(project.id)),
    }
    created = await client.post(
        "/api/v1/work-items",
        json={
            "title": "实时事件工作项",
            "description": "",
            "priority": "medium",
            "assignee_id": str(alice.id),
        },
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert created.status_code == 201, created.text
    ctx["item"] = created.json()
    return ctx


async def _subscribe(member_id: uuid.UUID) -> tuple[redis.Redis, redis.client.PubSub]:
    client = create_redis_client()
    pubsub = client.pubsub()
    await pubsub.subscribe(channel_for(member_id))
    return client, pubsub


async def _next_payload(pubsub: redis.client.PubSub, timeout: float = 5.0) -> dict:
    """在超时内等到一条频道消息并解析载荷。"""
    async with asyncio.timeout(timeout):
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message is not None and message.get("type") == "message":
                return json.loads(message["data"])


# ---------- Redis 频道事件（业务动作 commit 后发布） ----------


async def test_work_item_status_change_publishes_to_counterpart(
    client: httpx.AsyncClient, project: Project
) -> None:
    """负责人发布 → 主执行人频道收 work_item.published；主执行人开始 → 负责人频道收 work_item.started。"""
    ctx = await _setup(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    leader: ProjectMember = ctx["leader"]  # type: ignore[assignment]
    item = ctx["item"]

    alice_client, alice_pubsub = await _subscribe(alice.id)
    leader_client, leader_pubsub = await _subscribe(leader.id)
    try:
        published = await client.post(
            f"/api/v1/work-items/{item['id']}/publish",
            json={"version": 1},
            headers=ctx["leader_headers"],  # type: ignore[arg-type]
        )
        assert published.status_code == 200, published.text

        # 开发文档前置（设计 2026-07-30 §4.3）：负责人豁免后放行 start
        waived = await client.post(
            f"/api/v1/work-items/{item['id']}/dev-doc/waive",
            json={},
            headers=ctx["leader_headers"],  # type: ignore[arg-type]
        )
        assert waived.status_code == 200, waived.text

        payload = await _next_payload(alice_pubsub)
        assert payload["type"] == "work_item.published"
        assert set(payload) == {"id", "type", "project_id", "data", "created_at"}
        assert payload["project_id"] == str(project.id)
        assert payload["data"]["title"] == "工作项已发布"
        assert payload["data"]["link"] == f"/work-items/{item['id']}"
        uuid.UUID(payload["id"])  # id 为合法 uuid

        started = await client.post(
            f"/api/v1/work-items/{item['id']}/start",
            json={"version": 2},
            headers=ctx["alice_headers"],  # type: ignore[arg-type]
        )
        assert started.status_code == 200, started.text
        payload = await _next_payload(leader_pubsub)
        assert payload["type"] == "work_item.started"
        assert "爱丽丝" in payload["data"]["body"]
    finally:
        await alice_pubsub.aclose()
        await alice_client.aclose()
        await leader_pubsub.aclose()
        await leader_client.aclose()


async def test_collaboration_requested_publishes_only_to_recipient(
    client: httpx.AsyncClient, project: Project
) -> None:
    """发起协作请求 → 仅接收人频道收 collaboration.requested（他人频道无消息，16 节最小暴露）。"""
    ctx = await _setup(client, project)
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment]
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    item = ctx["item"]

    published = await client.post(
        f"/api/v1/work-items/{item['id']}/publish",
        json={"version": 1},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert published.status_code == 200, published.text

    bob_client, bob_pubsub = await _subscribe(bob.id)
    alice_client, alice_pubsub = await _subscribe(alice.id)
    try:
        created = await client.post(
            f"/api/v1/work-items/{item['id']}/collaboration-requests",
            json={
                "assignee_id": str(bob.id),
                "title": "整理语料",
                "goal": "整理 100 条语料",
            },
            headers=ctx["alice_headers"],  # type: ignore[arg-type]
        )
        assert created.status_code == 201, created.text

        payload = await _next_payload(bob_pubsub)
        assert payload["type"] == "collaboration.requested"
        assert payload["data"]["title"] == "新的协作请求"

        # 发起人自己的频道不应收到该事件（publish 的工作项事件已在订阅前发生）
        assert (
            await alice_pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0) is None
        )
    finally:
        await bob_pubsub.aclose()
        await bob_client.aclose()
        await alice_pubsub.aclose()
        await alice_client.aclose()


# ---------- SSE 端点 ----------


async def test_sse_requires_valid_token(client: httpx.AsyncClient, project: Project) -> None:
    """无 token → 401；伪造 token → 401（统一错误格式）。"""
    resp = await client.get("/api/v1/events/stream")
    assert resp.status_code == 401
    assert resp.json()["code"] == "INVALID_TOKEN"

    resp = await client.get("/api/v1/events/stream?token=not-a-token")
    assert resp.status_code == 401


async def test_sse_generator_stops_after_member_is_disabled(monkeypatch) -> None:
    """握手后成员资格失效时，生成器在下发下一条项目事件前关闭。"""
    from app.domains.notifications import stream as stream_module

    class FakeRequest:
        async def is_disconnected(self) -> bool:
            return False

    class FakePubSub:
        async def subscribe(self, _channel) -> None:
            return None

        async def get_message(self, **_kwargs):
            return {"data": '{"id":"evt-1","type":"work_item.updated"}'}

        async def unsubscribe(self, _channel) -> None:
            return None

        async def aclose(self) -> None:
            return None

    class FakeRedis:
        def pubsub(self):
            return FakePubSub()

        async def aclose(self) -> None:
            return None

    async def inactive(_member_id) -> bool:
        return False

    monkeypatch.setattr(stream_module, "create_redis_client", lambda: FakeRedis())
    monkeypatch.setattr(stream_module, "_stream_identity_is_active", inactive)
    generator = stream_module._event_generator(FakeRequest(), uuid.uuid4())
    assert await anext(generator) == ": connected\n\n"
    with pytest.raises(StopAsyncIteration):
        await anext(generator)


async def test_sse_stream_delivers_event(client: httpx.AsyncClient, project: Project) -> None:
    """?token= 建立流后，协作请求事件以 id:/event:/data: 帧数秒内下发。

    注：httpx 0.28 的 ASGITransport 会缓冲整个响应体，不支持流式响应，
    因此这里直接以裸 ASGI 调用真实 app（中间件/路由/端点/生成器全链路），
    收集 send 出的消息断言；传输层行为由 curl 实测（uvicorn/nginx）覆盖。
    """
    ctx = await _setup(client, project)
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment]
    item = ctx["item"]
    bob_token = ctx["bob_headers"]["Authorization"].removeprefix("Bearer ")  # type: ignore[index,union-attr]

    published = await client.post(
        f"/api/v1/work-items/{item['id']}/publish",
        json={"version": 1},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert published.status_code == 200, published.text

    messages: list[dict] = []

    async def receive() -> dict:
        await asyncio.sleep(3600)  # 客户端永不断开；is_disconnected 非阻塞探测返回 False
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/events/stream",
        "raw_path": b"/api/v1/events/stream",
        "query_string": f"token={bob_token}&project_id={project.id}".encode(),
        "headers": [
            (b"host", b"test"),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }
    app_task = asyncio.create_task(app(scope, receive, send))
    try:
        # 等连接建立：response.start + 首帧 ": connected"
        async with asyncio.timeout(5):
            while not any(m["type"] == "http.response.body" for m in messages):
                await asyncio.sleep(0.05)
        start = next(m for m in messages if m["type"] == "http.response.start")
        assert start["status"] == 200
        headers = {k.decode(): v.decode() for k, v in start["headers"]}
        assert headers["content-type"].startswith("text/event-stream")
        first_body = next(m for m in messages if m["type"] == "http.response.body")
        assert first_body["body"].decode().startswith(": connected")

        # 触发业务动作：alice 向 bob 发起协作请求
        created = await client.post(
            f"/api/v1/work-items/{item['id']}/collaboration-requests",
            json={
                "assignee_id": str(bob.id),
                "title": "整理语料",
                "goal": "整理 100 条语料",
            },
            headers=ctx["alice_headers"],  # type: ignore[arg-type]
        )
        assert created.status_code == 201, created.text

        # 数秒内收到 SSE 帧（id:/event:/data: 三段）
        frame = ""
        async with asyncio.timeout(5):
            while not frame:
                for m in messages:
                    if m["type"] == "http.response.body" and b"collaboration.requested" in m.get(
                        "body", b""
                    ):
                        frame = m["body"].decode()
                        break
                if not frame:
                    await asyncio.sleep(0.05)
        lines = frame.splitlines()
        assert lines[0].startswith("id: ")
        assert lines[1] == "event: collaboration.requested"
        assert lines[2].startswith("data: ")
        payload = json.loads(lines[2].removeprefix("data: "))
        assert payload["type"] == "collaboration.requested"
        assert payload["project_id"] == str(project.id)
        assert payload["data"]["title"] == "新的协作请求"
        assert payload["id"] == lines[0].removeprefix("id: ")
    finally:
        app_task.cancel()
        with suppress(asyncio.CancelledError):
            await app_task
