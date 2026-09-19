"""验证工作项 Agent 分析接口的投递行为与访问权限。

负责人和工作项相关成员可以触发分析；无关成员、未登录用户、无效工作项和
未注册 Agent 类型应得到对应的拒绝响应。
"""

import uuid

import httpx
import pytest
from sqlalchemy import select

from app.agents.graphs.base import AGENT_ROUTES
from app.agents.models import AgentRun, AgentSuggestion
from app.domains.collaboration.models import CollaborationRequest
from app.domains.project.models import Project
from app.domains.work_items.models import WorkItem, WorkItemCollaborator
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.queue.queue import QUEUE_KEY, dequeue
from tests.conftest import add_member, auth_headers

LEADER_PW = "Leader123!"
ALICE_PW = "Alice123!"
BOB_PW = "Bob123!"


async def _setup(client: httpx.AsyncClient, project: Project) -> dict[str, object]:
    """创建负责人、主执行人、无关成员及其测试工作项。"""
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
    """负责人触发分析后应创建待处理运行并投递队列任务。"""
    ctx = await _setup(client, project)
    redis_client = create_redis_client()
    try:
        # 隔离共享队列，避免其他用例遗留的任务影响本次消费。
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


@pytest.mark.parametrize("agent_type", ["dev_doc_review", "deliverable_review"])
async def test_related_member_can_trigger(
    client: httpx.AsyncClient, project: Project, agent_type: str
) -> None:
    """工作项主执行人应有权触发分析。"""
    ctx = await _setup(client, project)
    resp = await client.post(
        _url(ctx["item_id"]),  # type: ignore[arg-type]
        json={"agent_type": agent_type},
        headers=ctx["alice_headers"],  # type: ignore[arg-type]
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["agent_type"] == agent_type


async def test_unrelated_member_forbidden(client: httpx.AsyncClient, project: Project) -> None:
    """与工作项无关的成员不得触发分析。"""
    ctx = await _setup(client, project)
    resp = await client.post(
        _url(ctx["item_id"]),  # type: ignore[arg-type]
        json={"agent_type": "dev_doc_review"},
        headers=ctx["bob_headers"],  # type: ignore[arg-type]
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"


async def test_unknown_agent_type_rejected(client: httpx.AsyncClient, project: Project) -> None:
    """接口应拒绝未注册的 Agent 类型。"""
    ctx = await _setup(client, project)
    resp = await client.post(
        _url(ctx["item_id"]),  # type: ignore[arg-type]
        json={"agent_type": "not_a_real_agent"},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "VALIDATION_ERROR"


async def test_work_item_not_found(client: httpx.AsyncClient, project: Project) -> None:
    """引用不存在的工作项时应返回未找到。"""
    ctx = await _setup(client, project)
    resp = await client.post(
        _url("00000000-0000-0000-0000-000000000000"),
        json={"agent_type": "requirement_analyst"},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


async def test_unauthenticated_rejected(client: httpx.AsyncClient, project: Project) -> None:
    """未登录用户不得触发分析。"""
    ctx = await _setup(client, project)
    resp = await client.post(
        _url(ctx["item_id"]),  # type: ignore[arg-type]
        json={"agent_type": "requirement_analyst"},
    )
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "agent_type", sorted(set(AGENT_ROUTES) - {"dev_doc_review", "deliverable_review"})
)
async def test_member_management_agents_forbidden(
    client: httpx.AsyncClient, project: Project, agent_type: str
) -> None:
    ctx = await _setup(client, project)
    redis_client = create_redis_client()
    try:
        await redis_client.delete(QUEUE_KEY)
        for path in (_url(ctx["item_id"]), "/api/v1/agent-analysis"):
            response = await client.post(
                path, json={"agent_type": agent_type, "work_item_id": ctx["item_id"]},
                headers=ctx["alice_headers"],
            )
            assert response.status_code == 403, response.text
        async with async_session_factory() as session:
            assert list((await session.execute(select(AgentRun))).scalars()) == []

        created = await client.post(
            _url(ctx["item_id"]), json={"agent_type": agent_type},
            headers=ctx["leader_headers"],
        )
        assert created.status_code == 202, created.text
        run_id = uuid.UUID(created.json()["id"])
        async with async_session_factory() as session:
            run = await session.get(AgentRun, run_id)
            run.status = "failed"
            run.error = "test failure"
            await session.commit()
        await redis_client.delete(QUEUE_KEY)
        response = await client.post(
            f"/api/v1/agent-runs/{run_id}/retry", headers=ctx["alice_headers"]
        )
        assert response.status_code == 403
        assert await redis_client.llen(QUEUE_KEY) == 0
        async with async_session_factory() as session:
            run = await session.get(AgentRun, run_id)
            assert run.status == "failed"
            assert run.error == "test failure"
        response = await client.post(
            f"/api/v1/agent-runs/{run_id}/retry", headers=ctx["leader_headers"]
        )
        assert response.status_code == 202, response.text
    finally:
        await redis_client.aclose()


@pytest.mark.parametrize("relation", ["assignee", "collaborator", "requester", "recipient"])
@pytest.mark.parametrize("agent_type", ["dev_doc_review", "deliverable_review"])
async def test_related_member_trigger_retry_and_read_results(
    client: httpx.AsyncClient, project: Project, relation: str, agent_type: str
) -> None:
    ctx = await _setup(client, project)
    item_id = uuid.UUID(ctx["item_id"])
    alice, leader = ctx["alice"], ctx["leader"]
    async with async_session_factory() as session:
        item = await session.get(WorkItem, item_id)
        if relation != "assignee":
            item.assignee_id = leader.id
        if relation == "collaborator":
            session.add(WorkItemCollaborator(work_item_id=item_id, member_id=alice.id))
        if relation in {"requester", "recipient"}:
            # 发起人转派主任务后仍是协作请求的一方。
            session.add(CollaborationRequest(
                work_item_id=item_id,
                requester_id=alice.id if relation == "requester" else leader.id,
                assignee_id=alice.id if relation == "recipient" else leader.id,
                title="Review context", goal="Provide context", status="COMPLETED",
            ))
        await session.commit()
    response = await client.post(
        _url(ctx["item_id"]), json={"agent_type": agent_type}, headers=ctx["alice_headers"]
    )
    assert response.status_code == 202, response.text
    run_id = uuid.UUID(response.json()["id"])
    async with async_session_factory() as session:
        run = await session.get(AgentRun, run_id)
        run.status = "failed"
        await session.commit()
    denied = await client.post(
        f"/api/v1/agent-runs/{run_id}/retry", headers=ctx["bob_headers"]
    )
    assert denied.status_code == 403
    response = await client.post(
        f"/api/v1/agent-runs/{run_id}/retry", headers=ctx["alice_headers"]
    )
    assert response.status_code == 202, response.text
    assert response.json()["id"] == str(run_id)
    async with async_session_factory() as session:
        run = await session.get(AgentRun, run_id)
        run.status = "succeeded"
        suggestion = AgentSuggestion(
            run_id=run_id, suggestion_type=agent_type, content={"summary": "Reviewed"},
            confidence=0.8, risks="", fact_refs={}, prompt_version="test.v1",
        )
        session.add(suggestion)
        await session.commit()
        suggestion_id = str(suggestion.id)
    for path, expected_id in (("agent-runs", str(run_id)), ("agent-suggestions", suggestion_id)):
        response = await client.get(
            f"/api/v1/{path}?work_item_id={item_id}", headers=ctx["alice_headers"]
        )
        assert response.status_code == 200
        assert [row["id"] for row in response.json()] == [expected_id]
        denied = await client.get(
            f"/api/v1/{path}?work_item_id={item_id}", headers=ctx["bob_headers"]
        )
        assert denied.json() == []
    assert (await client.get(
        f"/api/v1/agent-runs/{run_id}", headers=ctx["alice_headers"]
    )).status_code == 200
    assert (await client.get(
        f"/api/v1/agent-runs/{run_id}", headers=ctx["bob_headers"]
    )).status_code == 404
