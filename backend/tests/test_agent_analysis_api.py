"""POST /work-items/{id}/agent-analysis 接口测试（12.5 节，T5.4）。

覆盖：
- 负责人 / 工作项相关成员（主执行人）触发 → 202，返回 agent_runs(pending)
  信息，队列出现 agent.run 任务；
- 无关成员 → 403；非项目成员 → 403；未登录 → 401；工作项不存在 → 404；
- 未注册的 agent_type → 400 VALIDATION_ERROR。
"""

import httpx
from sqlalchemy import select

from app.agents.models import AgentRun
from app.domains.project.models import Project
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.queue.queue import QUEUE_KEY, dequeue
from tests.conftest import add_member, auth_headers

LEADER_PW = "Leader123!"
ALICE_PW = "Alice123!"
BOB_PW = "Bob123!"


async def _setup(client: httpx.AsyncClient, project: Project) -> dict[str, object]:
    """准备：leader（负责人）+ alice（主执行人）/ bob（无关成员），并建一个工作项。"""
    _, leader = await add_member(project, "leader", LEADER_PW, role="leader", display_name="负责人")
    _, alice = await add_member(project, "alice", ALICE_PW, display_name="爱丽丝")
    _, bob = await add_member(project, "bob", BOB_PW, display_name="鲍勃")
    leader_headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id))
    created = await client.post(
        "/api/v1/work-items",
        json={"title": "RAG 工作项", "description": "实现 RAG", "assignee_id": str(alice.id)},
        headers=leader_headers,
    )
    assert created.status_code == 201, created.text
    return {
        "leader": leader,
        "alice": alice,
        "bob": bob,
        "item_id": created.json()["id"],
        "leader_headers": leader_headers,
        "alice_headers": await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id)),
        "bob_headers": await auth_headers(client, "bob", BOB_PW, project_id=str(project.id)),
    }


def _url(item_id: str) -> str:
    return f"/api/v1/work-items/{item_id}/agent-analysis"


async def test_leader_can_trigger_agent_analysis(
    client: httpx.AsyncClient, project: Project
) -> None:
    """负责人触发 → 202 + agent_runs(pending) 入库 + 队列投递 agent.run。"""
    ctx = await _setup(client, project)
    redis_client = create_redis_client()
    try:
        # 清空共享测试队列，避免其他用例残留任务干扰断言
        await redis_client.delete(QUEUE_KEY)
        resp = await client.post(
            _url(ctx["item_id"]),  # type: ignore[arg-type]
            json={"agent_type": "requirement_analyst", "prompt": "做一个 RAG 问答"},
            headers=ctx["leader_headers"],  # type: ignore[arg-type]
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["agent_type"] == "requirement_analyst"
        assert body["status"] == "pending"
        assert body["trigger_source"] == "manual"
        assert body["work_item_id"] == ctx["item_id"]
        assert body["request_id"]

        async with async_session_factory() as session:
            run = (await session.execute(select(AgentRun))).scalar_one()
        assert str(run.id) == body["id"]

        task = await dequeue(redis_client, timeout=2)
        assert task is not None
        assert task["type"] == "agent.run"
        assert task["payload"]["run_id"] == body["id"]
        assert task["payload"]["prompt"] == "做一个 RAG 问答"
    finally:
        await redis_client.aclose()


async def test_related_member_can_trigger(client: httpx.AsyncClient, project: Project) -> None:
    """工作项相关成员（主执行人）触发 → 202。"""
    ctx = await _setup(client, project)
    resp = await client.post(
        _url(ctx["item_id"]),  # type: ignore[arg-type]
        json={"agent_type": "assignment_advisor"},
        headers=ctx["alice_headers"],  # type: ignore[arg-type]
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["agent_type"] == "assignment_advisor"


async def test_unrelated_member_forbidden(client: httpx.AsyncClient, project: Project) -> None:
    """与工作项无关的成员触发 → 403。"""
    ctx = await _setup(client, project)
    resp = await client.post(
        _url(ctx["item_id"]),  # type: ignore[arg-type]
        json={"agent_type": "planning_advisor"},
        headers=ctx["bob_headers"],  # type: ignore[arg-type]
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"


async def test_unknown_agent_type_rejected(client: httpx.AsyncClient, project: Project) -> None:
    """未注册的 agent_type → 400 VALIDATION_ERROR。"""
    ctx = await _setup(client, project)
    resp = await client.post(
        _url(ctx["item_id"]),  # type: ignore[arg-type]
        json={"agent_type": "not_a_real_agent"},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "VALIDATION_ERROR"


async def test_work_item_not_found(client: httpx.AsyncClient, project: Project) -> None:
    """工作项不存在 → 404。"""
    ctx = await _setup(client, project)
    resp = await client.post(
        _url("00000000-0000-0000-0000-000000000000"),
        json={"agent_type": "requirement_analyst"},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


async def test_unauthenticated_rejected(client: httpx.AsyncClient, project: Project) -> None:
    """未登录 → 401。"""
    ctx = await _setup(client, project)
    resp = await client.post(
        _url(ctx["item_id"]),  # type: ignore[arg-type]
        json={"agent_type": "requirement_analyst"},
    )
    assert resp.status_code == 401
