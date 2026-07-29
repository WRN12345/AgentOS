"""T6.4 双任务并行端到端验收场景（18.3 节、第 22 章标准 7 之二）。

负责人同时创建两个互不依赖的工作项——RAG 任务（主执行人 alice，协作者 carol）
与 Agent 工具设计任务（主执行人 bob，协作者 dave）——并行推进，分别经历
协作、提交、审核直至完成，不形成内部 DAG（第 9 章并行场景说明）。

验证点（18.3 节）：
- 并行创建：同一 Idempotency-Key 并发重复提交只建一个工作项（17.2 节）；
- 两个工作项的状态、通知、审计事件互不串扰（审计目标集合不相交、
  协作者只收到本任务协作的通知）；
- 同一成员的负载在成员列表接口中正确汇总（推进中与完成后的计数变化）；
- 并行审批：负责人并发审核两个工作项，各自独立通过；
- Agent 建议生成但不改变正式业务状态（快照前后比对）；
- 两个工作项各自审计链完整，可独立回放。
"""

import asyncio

import httpx

from app.domains.project.models import Project, ProjectMember
from tests.helpers_e2e import (
    VALID_REQUIREMENT_OUTPUT,
    StubModelProvider,
    assert_audit_replay,
    assert_business_state_unchanged,
    drive_agent_run,
    get_suggestions_for_run,
    notifications_for,
    snapshot_business_state,
    all_audit_events,
    stub_provider,  # noqa: F401 - fixture 注入
)
from tests.helpers_t6a import (
    command_collaboration,
    command_work_item,
    create_collaboration,
    create_deliverable,
    get_work_item,
    make_ctx,
    storage,  # noqa: F401 - fixture 注入（文件存储指向临时目录）
)


async def _create_item(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    assignee_id: str,
    title: str,
    idempotency_key: str,
) -> httpx.Response:
    """负责人创建工作项（带幂等键）。"""
    return await client.post(
        "/api/v1/work-items",
        json={
            "title": title,
            "description": f"{title}描述",
            "acceptance_criteria": "验收通过",
            "priority": "high",
            "assignee_id": assignee_id,
            "due_at": "2026-08-01T00:00:00Z",
        },
        headers={**headers, "Idempotency-Key": idempotency_key},
    )


async def _member_loads(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, int]:
    """成员列表中的当前负载汇总：display_name -> active_work_items。"""
    resp = await client.get("/api/v1/members", headers=headers)
    assert resp.status_code == 200, resp.text
    return {m["display_name"]: m["active_work_items"] for m in resp.json()}


async def _collab_flow(
    client: httpx.AsyncClient,
    collaborator_headers: dict[str, str],
    collab_id: str,
    result_text: str,
) -> None:
    """协作者侧完整流转：接受 → 开始 → 回传（每个协作内部保持顺序）。"""
    resp = await command_collaboration(client, collaborator_headers, collab_id, "accept", 1)
    assert resp.status_code == 200, resp.text
    resp = await command_collaboration(client, collaborator_headers, collab_id, "start", 2)
    assert resp.status_code == 200, resp.text
    resp = await command_collaboration(
        client, collaborator_headers, collab_id, "submit", 3, result_text=result_text
    )
    assert resp.status_code == 200, resp.text


async def test_dual_items_parallel_end_to_end(
    client: httpx.AsyncClient,
    project: Project,
    storage,  # noqa: ANN001, ARG001 - fixture：存储指向临时目录
    stub_provider: StubModelProvider,
) -> None:
    ctx = await make_ctx(client, project)
    leader: ProjectMember = ctx["leader"]  # type: ignore[assignment]
    alice: ProjectMember = ctx["alice"]  # type: ignore[assignment] # RAG 任务主执行人
    bob: ProjectMember = ctx["bob"]  # type: ignore[assignment] # 工具设计任务主执行人
    carol: ProjectMember = ctx["carol"]  # type: ignore[assignment] # RAG 任务协作者
    dave: ProjectMember = ctx["dave"]  # type: ignore[assignment] # 工具设计任务协作者
    lh = ctx["leader_headers"]
    ah = ctx["alice_headers"]
    bh = ctx["bob_headers"]
    ch = ctx["carol_headers"]
    dh = ctx["dave_headers"]

    # ---- 步骤 1：并行创建两个工作项；同幂等键并发重复提交只生效一次 ----
    resp_a1, resp_a2, resp_b = await asyncio.gather(
        _create_item(client, lh, str(alice.id), "RAG 任务", "e2e-parallel-key-a"),
        _create_item(client, lh, str(alice.id), "RAG 任务", "e2e-parallel-key-a"),
        _create_item(client, lh, str(bob.id), "Agent 工具设计任务", "e2e-parallel-key-b"),
    )
    assert resp_a1.status_code == 201, resp_a1.text
    assert resp_a2.status_code == 201, resp_a2.text
    assert resp_b.status_code == 201, resp_b.text
    item_a = resp_a1.json()
    item_b = resp_b.json()
    item_a_id, item_b_id = item_a["id"], item_b["id"]
    assert resp_a2.json()["id"] == item_a_id  # 重复请求指向同一资源
    assert item_a_id != item_b_id
    events = await all_audit_events()
    assert [e.action for e in events] == ["work_item.created", "work_item.created"]
    assert len({str(e.target_id) for e in events}) == 2  # 只建了两个工作项

    # ---- 步骤 2：并行发布、并行开始 ----
    resp_pub_a, resp_pub_b = await asyncio.gather(
        command_work_item(client, lh, item_a_id, "publish", 1),
        command_work_item(client, lh, item_b_id, "publish", 1),
    )
    assert resp_pub_a.status_code == 200, resp_pub_a.text
    assert resp_pub_b.status_code == 200, resp_pub_b.text
    resp_start_a, resp_start_b = await asyncio.gather(
        command_work_item(client, ah, item_a_id, "start", 2),
        command_work_item(client, bh, item_b_id, "start", 2),
    )
    assert resp_start_a.status_code == 200, resp_start_a.text
    assert resp_start_b.status_code == 200, resp_start_b.text
    assert resp_start_a.json()["status"] == "IN_PROGRESS"
    assert resp_start_b.json()["status"] == "IN_PROGRESS"

    # 负载汇总：两位主执行人各 1 个进行中工作项，协作者为 0
    loads = await _member_loads(client, lh)
    assert loads["爱丽丝"] == 1
    assert loads["鲍勃"] == 1
    assert loads["卡罗尔"] == 0
    assert loads["戴夫"] == 0

    # ---- 步骤 3：并行发起各自协作（RAG 数据准备 / 工具评测用例） ----
    collab_a, collab_b = await asyncio.gather(
        create_collaboration(client, ah, item_a_id, carol.id, title="准备检索语料"),
        create_collaboration(client, bh, item_b_id, dave.id, title="设计工具评测用例"),
    )
    # ---- 步骤 4：两位协作者并行推进各自协作流，随后主执行人并行确认完成 ----
    await asyncio.gather(
        _collab_flow(client, ch, collab_a["id"], "语料已按模板整理"),
        _collab_flow(client, dh, collab_b["id"], "评测用例初稿完成"),
    )
    resp_ca, resp_cb = await asyncio.gather(
        command_collaboration(client, ah, collab_a["id"], "complete", 4),
        command_collaboration(client, bh, collab_b["id"], "complete", 4),
    )
    assert resp_ca.status_code == 200, resp_ca.text
    assert resp_cb.status_code == 200, resp_cb.text

    # ---- 步骤 5：并行推进中开启 Agent——建议生成，不改变正式业务状态 ----
    before = await snapshot_business_state([item_a_id, item_b_id])
    stub_provider.set_script(VALID_REQUIREMENT_OUTPUT)
    run = await drive_agent_run(item_a_id)
    assert run.status == "succeeded", run.error
    assert len(await get_suggestions_for_run(run.id)) == 1
    after = await snapshot_business_state([item_a_id, item_b_id])
    assert_business_state_unchanged(before, after)

    # ---- 步骤 6：并行提交交付物、并行提交审核 ----
    del_a, del_b = await asyncio.gather(
        create_deliverable(client, ah, item_a_id, type="text", content="RAG 评估结果 87%"),
        create_deliverable(
            client, bh, item_b_id, type="git_link", content="https://git.example.com/team/agent-tools.git"
        ),
    )
    resp_sub_a, resp_sub_b = await asyncio.gather(
        command_work_item(client, ah, item_a_id, "submit", 3),
        command_work_item(client, bh, item_b_id, "submit", 3),
    )
    assert resp_sub_a.status_code == 200, resp_sub_a.text
    assert resp_sub_b.status_code == 200, resp_sub_b.text

    # ---- 步骤 7：并行审批——负责人并发审核两个工作项，各自独立通过 ----
    resp_rev_a, resp_rev_b = await asyncio.gather(
        client.post(
            f"/api/v1/work-items/{item_a_id}/reviews",
            json={"deliverable_id": del_a["id"], "decision": "approve"},
            headers=lh,
        ),
        client.post(
            f"/api/v1/work-items/{item_b_id}/reviews",
            json={"deliverable_id": del_b["id"], "decision": "approve"},
            headers=lh,
        ),
    )
    assert resp_rev_a.status_code == 201, resp_rev_a.text
    assert resp_rev_b.status_code == 201, resp_rev_b.text
    current_a = await get_work_item(client, lh, item_a_id)
    current_b = await get_work_item(client, lh, item_b_id)
    assert current_a["status"] == "COMPLETED"
    assert current_b["status"] == "COMPLETED"
    # 主责任全程未串扰
    assert current_a["assignee"]["id"] == str(alice.id)
    assert current_b["assignee"]["id"] == str(bob.id)

    # 完成后负载归零
    loads = await _member_loads(client, lh)
    assert loads["爱丽丝"] == 0
    assert loads["鲍勃"] == 0

    # ---- 步骤 8：互不串扰断言——通知与审计目标集合 ----
    carol_notices = await notifications_for(carol.id)
    dave_notices = await notifications_for(dave.id)
    # 协作者收到的全部是本任务协作的通知（请求 + 完成回执），且标题不含对方任务内容
    assert {n.type for n in carol_notices} <= {"collaboration.requested", "collaboration.completed"}
    assert {n.type for n in dave_notices} <= {"collaboration.requested", "collaboration.completed"}
    assert any(n.type == "collaboration.requested" for n in carol_notices)
    assert any(n.type == "collaboration.requested" for n in dave_notices)
    assert all("评测用例" not in n.title for n in carol_notices)
    assert all("检索语料" not in n.title for n in dave_notices)

    events = await all_audit_events()
    a_targets = {item_a_id, collab_a["id"], del_a["id"]}
    b_targets = {item_b_id, collab_b["id"], del_b["id"]}
    assert a_targets.isdisjoint(b_targets)
    events_a = [e for e in events if str(e.target_id) in a_targets]
    events_b = [e for e in events if str(e.target_id) in b_targets]
    # 全部事件都归属于两个任务的审计链，无越界、无 Agent 业务事件
    assert len(events_a) + len(events_b) == len(events)
    assert all(not e.action.startswith("agent.") for e in events)

    # ---- 步骤 9：两个工作项各自审计链完整，可独立回放 ----
    assert_audit_replay(
        events_a,
        [
            ("work_item.created", item_a_id),
            ("work_item.published", item_a_id),
            ("work_item.started", item_a_id),
            ("collaboration.requested", collab_a["id"]),
            ("collaboration.accepted", collab_a["id"]),
            ("collaboration.started", collab_a["id"]),
            ("collaboration.submitted", collab_a["id"]),
            ("collaboration.completed", collab_a["id"]),
            ("deliverable.submitted", del_a["id"]),
            ("work_item.submitted", item_a_id),
            ("review.approved", item_a_id),
        ],
    )
    assert_audit_replay(
        events_b,
        [
            ("work_item.created", item_b_id),
            ("work_item.published", item_b_id),
            ("work_item.started", item_b_id),
            ("collaboration.requested", collab_b["id"]),
            ("collaboration.accepted", collab_b["id"]),
            ("collaboration.started", collab_b["id"]),
            ("collaboration.submitted", collab_b["id"]),
            ("collaboration.completed", collab_b["id"]),
            ("deliverable.submitted", del_b["id"]),
            ("work_item.submitted", item_b_id),
            ("review.approved", item_b_id),
        ],
    )
