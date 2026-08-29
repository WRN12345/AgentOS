"""并发、Agent 合约与 API 边界测试的共享辅助函数。

规则要求不改 conftest.py；这里只组合 conftest 提供的 add_member / auth_headers
与既有 API，做测试数据准备与 worker 直接驱动。
"""

import uuid

import httpx

from app.domains.project.models import Project, ProjectMember
from app.infrastructure.queue.queue import DELAYED_QUEUE_KEY, QUEUE_KEY
from app.workers.worker import handle_task
from tests.conftest import add_member, auth_headers

LEADER_PW = "Leader123!"
ALICE_PW = "Alice123!"
BOB_PW = "Bob123!"


async def setup_trio(client: httpx.AsyncClient, project: Project) -> dict[str, object]:
    """返回负责人、主执行人、普通成员及各自请求头。"""
    _, leader = await add_member(project, "leader", LEADER_PW, role="leader", display_name="负责人")
    _, alice = await add_member(project, "alice", ALICE_PW, display_name="爱丽丝")
    _, bob = await add_member(project, "bob", BOB_PW, display_name="鲍勃")
    return {
        "leader": leader,
        "alice": alice,
        "bob": bob,
        "leader_headers": await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id)),
        "alice_headers": await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id)),
        "bob_headers": await auth_headers(client, "bob", BOB_PW, project_id=str(project.id)),
    }


async def create_published_item(
    client: httpx.AsyncClient,
    leader_headers: dict[str, str],
    assignee_id: uuid.UUID,
    *,
    title: str = "RAG 工作项",
    due_at: str = "2026-08-01T00:00:00Z",
) -> dict:
    """创建并发布工作项，返回发布后的响应 JSON（version=2，状态 READY）。"""
    created = await client.post(
        "/api/v1/work-items",
        json={
            "title": title,
            "description": "实现 RAG",
            "assignee_id": str(assignee_id),
            "due_at": due_at,
        },
        headers=leader_headers,
    )
    assert created.status_code == 201, created.text
    published = await client.post(
        f"/api/v1/work-items/{created.json()['id']}/publish",
        json={"version": 1},
        headers=leader_headers,
    )
    assert published.status_code == 200, published.text
    return published.json()


async def create_transfer_request(
    client: httpx.AsyncClient,
    alice_headers: dict[str, str],
    item_id: str,
    to_member_id: uuid.UUID,
) -> dict:
    """alice 发起转派申请（PENDING，version=1），返回响应 JSON。"""
    resp = await client.post(
        f"/api/v1/work-items/{item_id}/transfer-requests",
        json={
            "to_member_id": str(to_member_id),
            "reason": "超出我的能力范围",
            "impact_note": "DDL 不变，进行中的协作请求由新负责人接管",
        },
        headers=alice_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def create_main_deadline_change(
    client: httpx.AsyncClient,
    alice_headers: dict[str, str],
    item_id: str,
    new_due_at: str = "2026-08-15T00:00:00Z",
) -> dict:
    """alice 发起主任务 DDL 变更（PENDING_APPROVAL，version=1），返回响应 JSON。"""
    resp = await client.post(
        f"/api/v1/work-items/{item_id}/deadline-change-requests",
        json={
            "target_type": "work_item",
            "target_id": item_id,
            "new_due_at": new_due_at,
            "reason": "依赖方延期，需要顺延",
        },
        headers=alice_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "PENDING_APPROVAL", body
    return body


async def run_agent_once(redis_client, run_id: uuid.UUID, prompt: str = "") -> None:  # noqa: ANN001
    """直接调用 worker 处理函数执行一次 agent.run（不真起进程）。

    执行前清空即时/延迟队列，避免 request_agent_analysis 投递的残留任务干扰断言。
    """
    await redis_client.delete(QUEUE_KEY, DELAYED_QUEUE_KEY)
    await handle_task(
        {
            "id": str(uuid.uuid4()),
            "type": "agent.run",
            "payload": {"run_id": str(run_id), "prompt": prompt},
        },
        redis_client,
    )
