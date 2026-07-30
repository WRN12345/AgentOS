"""T6.3 RAG 串行端到端验收场景（设计文档第 9 章时序、18.3 节、第 22 章标准 7）。

一个测试函数内跑完整个第 9 章时序（conftest 每个用例后 TRUNCATE 清库，
跨函数无法保留场景状态）：

    负责人 L → 后端开发 B：分配 RAG 工作项
    B → L：申请转派给 RAG 工程师 R（转派必须审批：审批前主责任不转移）
    L 审批通过：主责任转移，历史负责人完整可查
    R → 销售专家 S：协作请求 1（按模板整理销售资料）→ S 接受并回传
    R → S：协作请求 2（人工标注测试集）→ S 接受并回传（含标注文件）
    （协作请求无需负责人审批：负责人待审批列表始终不含协作）
    R → L：提交 Git 链接 + 评估结果文本 + 说明文件，工作项提交审核
    L 审核通过：工作项 COMPLETED（终态即归档，只读）
    R 上传归档证据文件（“勾选已同步”为飞书手工步骤，平台以终态归档 +
    证据文件留痕表达）

验证点（18.3 节）：
- 每一步产生对应审计事件；该有站内通知的步骤通知到正确接收人
  （工作项状态命令按设计只发 SSE 实时事件、不写站内通知，见
  domains/work_items/service.py._build_status_events 注释）；
- 协作请求无需负责人审批即可进行、转派必须审批；
- 历史负责人完整可查（转派历史接口 + work_item.assignee_changed 审计）；
- 场景中段开启 Agent（替身 ModelProvider 驱动 agent run）：建议生成并通知
  负责人，但工作项/审批等正式业务状态只由人的操作改变（快照前后比对）；
- 场景末尾按审计事件完整回放第 9 章时序，与预期步骤逐一比对。
"""

import uuid

import httpx

from app.domains.dev_docs.models import DevDoc
from app.domains.project.models import Project, ProjectMember
from app.infrastructure.database.engine import async_session_factory
from tests.helpers_e2e import (
    VALID_REQUIREMENT_OUTPUT,
    StubModelProvider,
    assert_audit_replay,
    assert_business_state_unchanged,
    assert_notification,
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
    create_transfer,
    create_work_item,
    get_work_item,
    make_ctx,
    publish_work_item,
    storage,  # noqa: F401 - fixture 注入（文件存储指向临时目录）
    upload_file,
)

ITEM_TITLE = "RAG 工作项"


async def _approvals(client: httpx.AsyncClient, headers: dict[str, str]) -> list[dict]:
    """负责人待审批聚合列表。"""
    resp = await client.get("/api/v1/approvals", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_rag_serial_end_to_end(
    client: httpx.AsyncClient,
    project: Project,
    storage,  # noqa: ANN001, ARG001 - fixture：存储指向临时目录
    stub_provider: StubModelProvider,
) -> None:
    ctx = await make_ctx(client, project)
    leader: ProjectMember = ctx["leader"]  # type: ignore[assignment] # 负责人 L
    backend_dev: ProjectMember = ctx["alice"]  # type: ignore[assignment] # 后端开发 B
    rag_eng: ProjectMember = ctx["bob"]  # type: ignore[assignment] # RAG 工程师 R
    sales: ProjectMember = ctx["carol"]  # type: ignore[assignment] # 销售专家 S
    lh = ctx["leader_headers"]
    bh = ctx["alice_headers"]
    rh = ctx["bob_headers"]
    sh = ctx["carol_headers"]

    # ---- 步骤 1：负责人分配 RAG 工作项（后端开发为主执行人）并发布 ----
    item = await create_work_item(client, lh, backend_dev.id, title=ITEM_TITLE)
    item_id = item["id"]
    item = await publish_work_item(client, lh, item)
    assert item["status"] == "READY"
    assert item["assignee"]["id"] == str(backend_dev.id)

    events = await all_audit_events()
    assert [(e.action, str(e.target_id)) for e in events] == [
        ("work_item.created", item_id),
        ("work_item.published", item_id),
    ]
    created_event = events[0]
    assert created_event.after["assignee_id"] == str(backend_dev.id)

    # ---- 步骤 2：后端开发申请转派给 RAG 工程师（转派必须审批） ----
    resp = await create_transfer(client, bh, item_id, rag_eng.id)
    assert resp.status_code == 201, resp.text
    transfer = resp.json()
    transfer_id = transfer["id"]
    assert transfer["status"] == "PENDING"

    # 审批前主责任不转移（7.3 节：转派必须审批才生效）
    current = await get_work_item(client, lh, item_id)
    assert current["assignee"]["id"] == str(backend_dev.id)
    # 负责人待审批列表中出现该转派申请
    approvals = await _approvals(client, lh)
    assert [a["id"] for a in approvals] == [transfer_id]
    assert approvals[0]["kind"] == "transfer"

    events = await all_audit_events()
    assert events[-1].action == "transfer.requested"
    assert str(events[-1].target_id) == transfer_id
    leader_notices = await notifications_for(leader.id)
    assert_notification(leader_notices, type="transfer.requested")

    # ---- 步骤 3：负责人审批通过（主责任转移，历史负责人可查） ----
    resp = await client.post(
        f"/api/v1/transfer-requests/{transfer_id}/approve",
        json={"version": 1, "decision_note": "同意，RAG 部分由专人负责"},
        headers=lh,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "APPROVED"

    current = await get_work_item(client, lh, item_id)
    assert current["assignee"]["id"] == str(rag_eng.id)  # 主责任转移
    assert current["version"] == 3  # create v1 → publish v2 → approve v3

    # 历史负责人完整可查：转派历史接口 + 审计 before/after 双侧留痕
    resp = await client.get(f"/api/v1/work-items/{item_id}/transfer-requests", headers=rh)
    assert resp.status_code == 200, resp.text
    history = resp.json()
    assert len(history) == 1
    assert history[0]["from_member"]["id"] == str(backend_dev.id)
    assert history[0]["to_member"]["id"] == str(rag_eng.id)
    assert history[0]["status"] == "APPROVED"

    events = await all_audit_events()
    tail = [(e.action, str(e.target_id)) for e in events[-2:]]
    assert sorted(tail) == sorted(
        [("transfer.approved", transfer_id), ("work_item.assignee_changed", item_id)]
    )
    assignee_event = next(e for e in events if e.action == "work_item.assignee_changed")
    assert assignee_event.before == {"assignee_id": str(backend_dev.id)}  # 历史负责人
    assert assignee_event.after["assignee_id"] == str(rag_eng.id)
    approve_event = next(e for e in events if e.action == "transfer.approved")
    assert approve_event.after["decision_note"] == "同意，RAG 部分由专人负责"

    # 新旧主执行人均收到审批结果通知；待审批列表清空
    assert_notification(
        await notifications_for(backend_dev.id), type="transfer.approved", title_contains="已通过"
    )
    assert_notification(
        await notifications_for(rag_eng.id), type="transfer.approved", title_contains="主执行人"
    )
    assert await _approvals(client, lh) == []

    # ---- 步骤 4：RAG 工程师开始执行 ----
    # 开发文档前置（设计 2026-07-30 §4.3）：直接建库豁免行放行 start——
    # 本场景做精确审计链回放，走 waive 接口会引入与场景无关的 dev_doc 事件
    async with async_session_factory() as session:
        session.add(DevDoc(work_item_id=uuid.UUID(item_id), waived=True))
        await session.commit()
    resp = await command_work_item(client, rh, item_id, "start", 3)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "IN_PROGRESS"
    events = await all_audit_events()
    assert (events[-1].action, str(events[-1].target_id)) == ("work_item.started", item_id)

    # ---- 步骤 5-6：协作请求 1（按模板整理销售资料），无需负责人审批 ----
    collab1 = await create_collaboration(
        client, rh, item_id, sales.id, title="按模板整理销售资料"
    )
    assert collab1["status"] == "REQUESTED"
    # 协作请求无需负责人审批：负责人待审批列表不出现协作请求
    assert await _approvals(client, lh) == []
    events = await all_audit_events()
    assert (events[-1].action, str(events[-1].target_id)) == (
        "collaboration.requested",
        collab1["id"],
    )
    assert_notification(await notifications_for(sales.id), type="collaboration.requested")

    # 销售专家接受 → 开始 → 回传整理后的资料 → RAG 工程师确认完成
    resp = await command_collaboration(client, sh, collab1["id"], "accept", 1)
    assert resp.status_code == 200, resp.text
    resp = await command_collaboration(client, sh, collab1["id"], "start", 2)
    assert resp.status_code == 200, resp.text
    resp = await command_collaboration(
        client, sh, collab1["id"], "submit", 3, result_text="整理后的销售资料（按模板）"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "SUBMITTED"
    assert_notification(
        await notifications_for(rag_eng.id), type="collaboration.submitted"
    )
    resp = await command_collaboration(client, rh, collab1["id"], "complete", 4)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "COMPLETED"

    # ---- 步骤 7：协作请求 2（人工标注测试集），回传含标注结果文件 ----
    collab2 = await create_collaboration(
        client, rh, item_id, sales.id, title="人工标注测试集"
    )
    resp = await command_collaboration(client, sh, collab2["id"], "accept", 1)
    assert resp.status_code == 200, resp.text
    resp = await command_collaboration(client, sh, collab2["id"], "start", 2)
    assert resp.status_code == 200, resp.text
    # 销售专家上传标注结果文件并随回传引用
    resp = await upload_file(
        client,
        sh,
        content=b"query,expected_answer\nq1,a1\n",
        filename="labeled_testset.csv",
        mime="text/csv",
        work_item_id=item_id,
    )
    assert resp.status_code == 201, resp.text
    labels_file = resp.json()
    resp = await command_collaboration(
        client,
        sh,
        collab2["id"],
        "submit",
        3,
        result_text="标注完成，结果见附件 CSV",
        file_id=labels_file["id"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result_file_id"] == labels_file["id"]
    resp = await command_collaboration(client, rh, collab2["id"], "complete", 4)
    assert resp.status_code == 200, resp.text

    # ---- 步骤 8：场景中开启 Agent——建议生成，但不改变正式业务状态 ----
    before = await snapshot_business_state([item_id])
    stub_provider.set_script(VALID_REQUIREMENT_OUTPUT)
    run = await drive_agent_run(item_id)
    assert run.status == "succeeded", run.error
    suggestions = await get_suggestions_for_run(run.id)
    assert len(suggestions) == 1  # Agent 建议已生成
    assert suggestions[0].suggestion_type == "requirement"
    # 建议就绪通知负责人查看（10.2 节流程末节点）
    assert_notification(
        await notifications_for(leader.id), type="agent.suggestion_ready"
    )
    # 正式业务状态（工作项状态/版本、审计事件）与 Agent 运行前完全一致
    after = await snapshot_business_state([item_id])
    assert_business_state_unchanged(before, after)
    current = await get_work_item(client, lh, item_id)
    assert current["status"] == "IN_PROGRESS"
    assert current["assignee"]["id"] == str(rag_eng.id)

    # ---- 步骤 9：RAG 工程师提交交付（Git 链接 + 评估结果文本 + 说明文件）----
    git_deliverable = await create_deliverable(
        client, rh, item_id, type="git_link", content="https://git.example.com/team/rag.git"
    )
    eval_deliverable = await create_deliverable(
        client, rh, item_id, type="text", content="评估结果：测试集准确率 87%"
    )
    resp = await upload_file(
        client,
        rh,
        content=b"RAG implementation notes\n",
        filename="delivery_notes.txt",
        mime="text/plain",
        work_item_id=item_id,
    )
    assert resp.status_code == 201, resp.text
    notes_file = resp.json()
    notes_deliverable = await create_deliverable(
        client, rh, item_id, type="file", file_id=notes_file["id"]
    )
    assert notes_deliverable["file"]["sha256"] == notes_file["sha256"]
    assert [git_deliverable["version"], eval_deliverable["version"], notes_deliverable["version"]] == [1, 2, 3]

    # 提交审核：IN_PROGRESS → IN_REVIEW
    resp = await command_work_item(client, rh, item_id, "submit", 4)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "IN_REVIEW"
    events = await all_audit_events()
    assert (events[-1].action, str(events[-1].target_id)) == ("work_item.submitted", item_id)

    # ---- 步骤 10：负责人审核通过（IN_REVIEW → COMPLETED） ----
    resp = await client.post(
        f"/api/v1/work-items/{item_id}/reviews",
        json={"deliverable_id": git_deliverable["id"], "decision": "approve"},
        headers=lh,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["work_item_status"] == "COMPLETED"
    current = await get_work_item(client, lh, item_id)
    assert current["status"] == "COMPLETED"
    assert_notification(
        await notifications_for(rag_eng.id), type="review.approved", title_contains="审核通过"
    )

    # ---- 步骤 11：勾选“已同步”并归档证据 ----
    # “在飞书项目群手工同步 / 勾选已同步”是系统外手工步骤（第 9 章），平台侧
    # 以 COMPLETED 终态（归档只读）+ 归档证据文件留痕表达。
    resp = await upload_file(
        client,
        rh,
        content=b"feishu sync done 2026-07-28\n",
        filename="archive_evidence.txt",
        mime="text/plain",
        work_item_id=item_id,
    )
    assert resp.status_code == 201, resp.text
    evidence_file = resp.json()

    # 归档后只读：终态工作项拒绝再提交、拒绝新增交付物
    resp = await command_work_item(client, rh, item_id, "submit", current["version"])
    assert resp.status_code == 409, resp.text
    resp = await client.post(
        f"/api/v1/work-items/{item_id}/deliverables",
        json={"type": "text", "content": "归档后补交"},
        headers=rh,
    )
    assert resp.status_code == 409, resp.text

    # ---- 收尾：按审计事件完整回放第 9 章时序，与预期步骤逐一比对 ----
    # 说明：同一事务写入的事件（transfer.approved 与 work_item.assignee_changed）
    # created_at 相同，断言按"同刻分组"比对（组间顺序严格、组内无序）。
    events = await all_audit_events()
    expected = [
        ("work_item.created", item_id),  # L 分配 RAG 工作项（B 为主执行人）
        ("work_item.published", item_id),  # L 发布
        ("transfer.requested", transfer_id),  # B 申请转派给 R
        ("transfer.approved", transfer_id),  # L 审批通过（与下一条同事务）
        ("work_item.assignee_changed", item_id),  # 主责任 B → R（历史留痕）
        ("work_item.started", item_id),  # R 开始执行
        ("collaboration.requested", collab1["id"]),  # R→S 协作 1：整理资料
        ("collaboration.accepted", collab1["id"]),
        ("collaboration.started", collab1["id"]),
        ("collaboration.submitted", collab1["id"]),  # S 回传整理后的资料
        ("collaboration.completed", collab1["id"]),
        ("collaboration.requested", collab2["id"]),  # R→S 协作 2：标注测试集
        ("collaboration.accepted", collab2["id"]),
        ("collaboration.started", collab2["id"]),
        ("file.uploaded", labels_file["id"]),  # S 上传标注结果文件
        ("collaboration.submitted", collab2["id"]),  # S 回传标注结果
        ("collaboration.completed", collab2["id"]),
        ("deliverable.submitted", git_deliverable["id"]),  # R 提交 Git 链接
        ("deliverable.submitted", eval_deliverable["id"]),  # R 提交评估结果
        ("file.uploaded", notes_file["id"]),  # R 上传说明文件
        ("deliverable.submitted", notes_deliverable["id"]),  # R 提交说明（文件）
        ("work_item.submitted", item_id),  # R 提交审核
        ("review.approved", item_id),  # L 审核通过（COMPLETED=归档）
        ("file.uploaded", evidence_file["id"]),  # R 归档证据（飞书已同步）
    ]
    assert_audit_replay(events, expected)

    # 回放确认的每一步都归属于本场景对象，无任何 Agent 造成的业务状态变更：
    # 上面的 expected 不含 agent.* 业务动作，事件总数相等即证明无多余事件。
    assert all(not e.action.startswith("agent.") for e in events)
