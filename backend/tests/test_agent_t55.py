"""验证风险、交付物初审和项目总结 Agent 的端到端行为。

风险扫描应按项目去重并通知负责人；初审只能生成建议，不能修改正式评审或工作项
状态，投递失败也不得影响提交主流程；总结必须基于真实项目数据。文件交付物仅可
向模型暴露元数据，不能读取原文。
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.agents.models import AgentRun, AgentSuggestion
from app.agents.service import request_agent_analysis
from app.agents.specialists import review, risk, summary
from app.agents.tools import TOOL_REGISTRY
from app.domains.deliverables.models import Deliverable
from app.domains.files.models import StoredFile
from app.domains.notifications.models import Notification
from app.domains.project.models import Project, ProjectMember
from app.domains.reviews.models import Review
from app.domains.transfers.models import TransferRequest
from app.domains.work_items.models import WorkItem
from app.domains.work_items.service import run_command
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.queue.queue import QUEUE_KEY, dequeue
from app.workers.risk_scan import run_risk_scan
from app.workers.worker import handle_task
from tests.conftest import add_member, auth_headers

LEADER_PW = "Leader123!"
ALICE_PW = "Alice123!"


class _FakeProvider:
    """返回固定 JSON 并记录调用参数的模型替身。"""

    name = "fake"
    model = "fake-model"
    is_external = False

    def __init__(self, response: str):
        self.response = response
        self.calls: list[dict] = []

    async def generate(
        self, prompt: str, *, system: str | None = None, json_output: bool = False
    ) -> str:
        self.calls.append({"prompt": prompt, "system": system, "json_output": json_output})
        return self.response


def _patch_provider(monkeypatch: pytest.MonkeyPatch, provider: _FakeProvider) -> None:
    monkeypatch.setattr("app.agents.specialists.common.get_model_provider", lambda: provider)


async def _make_work_item(
    assignee_id: uuid.UUID,
    title: str,
    *,
    project_id: uuid.UUID,
    status: str = "READY",
    due_at: datetime | None = None,
    acceptance_criteria: str | None = None,
) -> WorkItem:
    async with async_session_factory() as session:
        item = WorkItem(
            title=title,
            description="描述",
            project_id=project_id,
            assignee_id=assignee_id,
            status=status,
            due_at=due_at,
            acceptance_criteria=acceptance_criteria,
        )
        item.collaborators = []
        session.add(item)
        await session.commit()
        return item


async def _get_run(run_id: uuid.UUID) -> AgentRun:
    async with async_session_factory() as session:
        run = await session.get(AgentRun, run_id)
        assert run is not None
        return run


async def _reviews_count() -> int:
    async with async_session_factory() as session:
        return (await session.execute(select(func.count()).select_from(Review))).scalar_one()


async def test_risk_scan_generates_risk_suggestion_and_notifies_leader(
    project: Project, leader: ProjectMember, monkeypatch: pytest.MonkeyPatch
) -> None:
    """风险扫描应识别临期和阻塞项、通知负责人并去重活跃运行。"""
    now = datetime.now(UTC)
    due_item = await _make_work_item(
        leader.id, "临期工作项", project_id=project.id, status="IN_PROGRESS", due_at=now + timedelta(hours=2)
    )
    blocked_item = await _make_work_item(leader.id, "阻塞工作项", project_id=project.id, status="BLOCKED")

    provider = _FakeProvider(
        json.dumps(
            {
                "content": {
                    "summary": "发现 2 项风险",
                    "rationale": "存在临期与阻塞工作项",
                    "risks": [
                        {
                            "type": "overdue",
                            "target_type": "work_item",
                            "target_id": str(due_item.id),
                            "title": "临期工作项",
                            "severity": "medium",
                            "detail": "距截止时间不足 24 小时",
                        },
                        {
                            "type": "blocked",
                            "target_type": "work_item",
                            "target_id": str(blocked_item.id),
                            "title": "阻塞工作项",
                            "severity": "high",
                            "detail": "工作项处于阻塞状态",
                        },
                    ],
                },
                "confidence": 0.8,
                "risks": "未覆盖无 DDL 的长期 READY 工作项",
            },
            ensure_ascii=False,
        )
    )
    _patch_provider(monkeypatch, provider)

    redis_client = create_redis_client()
    try:
        await redis_client.delete(QUEUE_KEY)  # 隔离共享队列中的遗留任务。

        # 直接调用周期任务入口，避免依赖独立 scheduler 进程。
        result = await run_risk_scan(redis_client)
        assert result["status"] == "done"
        assert result["skipped"] == []
        assert [e["project_id"] for e in result["enqueued"]] == [str(project.id)]
        run_id = uuid.UUID(result["enqueued"][0]["run_id"])

        # 同项目已有活跃风险运行时，下一轮扫描不得重复投递。
        again = await run_risk_scan(redis_client)
        assert again["status"] == "done"
        assert again["skipped"] == [str(project.id)]
        assert again["enqueued"] == []

        task = await dequeue(redis_client, timeout=2)
        assert task is not None and task["type"] == "agent.run"
        await handle_task(task, redis_client)

        run = await _get_run(run_id)
        assert run.status == "succeeded", run.error
        assert run.agent_type == risk.AGENT_TYPE == "workflow_risk"
        assert run.trigger_source == "scheduler"
        assert run.work_item_id is None

        async with async_session_factory() as session:
            suggestion = (
                await session.execute(
                    select(AgentSuggestion).where(AgentSuggestion.run_id == run.id)
                )
            ).scalar_one()
        assert suggestion.suggestion_type == risk.SUGGESTION_TYPE == "risk"
        assert suggestion.prompt_version == risk.PROMPT_VERSION == "workflow_risk.v1"
        content = suggestion.content
        assert content["summary"] == "发现 2 项风险"
        assert [r["type"] for r in content["risks"]] == ["overdue", "blocked"]
        assert content["risks"][0]["severity"] == "medium"
        assert content["risks"][1]["target_id"] == str(blocked_item.id)
        assert set(suggestion.fact_refs["work_item_ids"]) == {
            str(due_item.id),
            str(blocked_item.id),
        }

        async with async_session_factory() as session:
            notification = (
                await session.execute(
                    select(Notification).where(Notification.type == "agent.suggestion_ready")
                )
            ).scalar_one()
        assert notification.recipient_id == leader.id

        # 模型上下文必须来自当前项目的真实风险项。
        prompt_sent = provider.calls[0]["prompt"]
        assert "临期工作项" in prompt_sent and "阻塞工作项" in prompt_sent
        assert provider.calls[0]["json_output"] is True
    finally:
        await redis_client.aclose()


async def _prepare_submittable_item(
    assignee: ProjectMember, *, acceptance_criteria: str, deliverable_content: str
) -> WorkItem:
    """创建包含首版文本交付物的进行中工作项。"""
    item = await _make_work_item(
        assignee.id,
        "实现登录接口",
        project_id=assignee.project_id,
        status="IN_PROGRESS",
        acceptance_criteria=acceptance_criteria,
    )
    async with async_session_factory() as session:
        session.add(
            Deliverable(
                # 交付物项目归属与所属工作项保持一致。
                project_id=item.project_id,
                work_item_id=item.id,
                type="text",
                content=deliverable_content,
                version=1,
                submitted_by=assignee.id,
            )
        )
        await session.commit()
    return item


async def test_submit_triggers_deliverable_review_without_touching_reviews(
    project: Project, leader: ProjectMember, monkeypatch: pytest.MonkeyPatch
) -> None:
    """提交后应异步生成初审建议，且不得写入正式评审表。"""
    _, alice = await add_member(project, "alice", ALICE_PW, display_name="爱丽丝")
    item = await _prepare_submittable_item(
        alice,
        acceptance_criteria="账号密码可登录并返回令牌",
        deliverable_content="实现说明：已完成登录接口，返回 JWT 令牌",
    )

    provider = _FakeProvider(
        json.dumps(
            {
                "content": {
                    "summary": "初审基本通过，1 项需人工核实",
                    "rationale": "交付物覆盖登录与令牌返回",
                    "checklist": [
                        {
                            "checkpoint": "账号密码可登录",
                            "verdict": "pass",
                            "evidence": "交付物说明已完成登录接口",
                        },
                        {
                            "checkpoint": "返回令牌",
                            "verdict": "uncertain",
                            "evidence": "说明提到 JWT 令牌，但未附验证结果",
                        },
                    ],
                },
                "confidence": 0.7,
                "risks": "仅凭文本说明初审，未实际运行接口",
            },
            ensure_ascii=False,
        )
    )
    _patch_provider(monkeypatch, provider)

    redis_client = create_redis_client()
    try:
        await redis_client.delete(QUEUE_KEY)

        async with async_session_factory() as session:
            out = await run_command(session, alice, item.id, "submit", item.version)
        assert out.status == "IN_REVIEW"

        task = await dequeue(redis_client, timeout=2)
        assert task is not None and task["type"] == "agent.run"
        run_id = uuid.UUID(task["payload"]["run_id"])
        run = await _get_run(run_id)
        assert run.agent_type == review.AGENT_TYPE == "deliverable_review"
        assert run.trigger_source == "event"
        assert run.work_item_id == item.id

        await handle_task(task, redis_client)

        run = await _get_run(run_id)
        assert run.status == "succeeded", run.error
        async with async_session_factory() as session:
            suggestion = (
                await session.execute(
                    select(AgentSuggestion).where(AgentSuggestion.run_id == run.id)
                )
            ).scalar_one()
        assert suggestion.suggestion_type == review.SUGGESTION_TYPE == "review"
        assert suggestion.prompt_version == review.PROMPT_VERSION == "deliverable_review.v1"
        checklist = suggestion.content["checklist"]
        assert [c["verdict"] for c in checklist] == ["pass", "uncertain"]
        assert checklist[0]["checkpoint"] and checklist[0]["evidence"]
        assert suggestion.fact_refs["work_item_ids"] == [str(item.id)]
        assert len(suggestion.fact_refs["deliverable_ids"]) == 1

        # 文本交付物的最小上下文包含验收标准和正文。
        prompt_sent = provider.calls[0]["prompt"]
        assert "账号密码可登录并返回令牌" in prompt_sent
        assert "实现说明：已完成登录接口" in prompt_sent

        # Agent 初审只能提供建议，不得触碰正式评审和工作项状态。
        assert await _reviews_count() == 0
        async with async_session_factory() as session:
            final_item = await session.get(WorkItem, item.id)
            assert final_item is not None and final_item.status == "IN_REVIEW"
    finally:
        await redis_client.aclose()


async def test_submit_survives_review_dispatch_failure(
    project: Project, leader: ProjectMember, monkeypatch: pytest.MonkeyPatch
) -> None:
    """初审投递失败时，提交主流程仍应进入待评审状态。"""
    _, alice = await add_member(project, "alice", ALICE_PW, display_name="爱丽丝")
    item = await _prepare_submittable_item(
        alice, acceptance_criteria="可登录", deliverable_content="实现说明"
    )

    async def _boom(*args, **kwargs):  # noqa: ANN202
        raise RuntimeError("queue down")

    monkeypatch.setattr("app.domains.work_items.service.request_agent_analysis", _boom)

    async with async_session_factory() as session:
        out = await run_command(session, alice, item.id, "submit", item.version)
    assert out.status == "IN_REVIEW"

    async with async_session_factory() as session:
        run_count = (
            await session.execute(select(func.count()).select_from(AgentRun))
        ).scalar_one()
    assert run_count == 0


async def test_summary_agent_uses_real_stats(
    project: Project, leader: ProjectMember, monkeypatch: pytest.MonkeyPatch
) -> None:
    """项目摘要应使用数据库真实统计，并引用对应实体 ID。"""
    _, alice = await add_member(project, "alice", ALICE_PW, display_name="爱丽丝")
    completed = await _make_work_item(alice.id, "已完成事项", project_id=project.id, status="COMPLETED")
    in_review = await _make_work_item(alice.id, "待审工作项", project_id=project.id, status="IN_REVIEW")
    await _make_work_item(alice.id, "阻塞工作项", project_id=project.id, status="BLOCKED")
    await _make_work_item(alice.id, "待启动工作项", project_id=project.id, status="READY")
    async with async_session_factory() as session:
        transfer = TransferRequest(
            work_item_id=in_review.id,
            from_member_id=alice.id,
            to_member_id=leader.id,
            reason="精力不足",
            impact_note="不影响 DDL",
        )
        session.add(transfer)
        await session.commit()
        transfer_id = transfer.id

    provider = _FakeProvider(
        json.dumps(
            {
                "content": {
                    "summary": "项目推进中，1 项完成、2 项待审批",
                    "rationale": "基于状态统计与待审批清单",
                    "progress": "4 个工作项：1 完成、1 待审、1 阻塞、1 待启动",
                    "completed": ["已完成事项"],
                    "pending_approvals": ["待审工作项待负责人审核", "1 项转派待审批"],
                    "risks": ["1 个工作项阻塞中"],
                },
                "confidence": 0.9,
                "risks": "摘要基于当前快照，之后的变化需重新生成",
            },
            ensure_ascii=False,
        )
    )
    _patch_provider(monkeypatch, provider)

    redis_client = create_redis_client()
    try:
        await redis_client.delete(QUEUE_KEY)
        async with async_session_factory() as session:
            run = await request_agent_analysis(
                session,
                redis_client,
                agent_type=summary.AGENT_TYPE,
                project_id=project.id,  # 项目归属用于约束工具查询范围。
                trigger_source="manual",
            )
        await handle_task(
            {"id": str(uuid.uuid4()), "type": "agent.run", "payload": {"run_id": str(run.id)}},
            redis_client,
        )

        run = await _get_run(run.id)
        assert run.status == "succeeded", run.error
        assert run.agent_type == "summary_agent"

        async with async_session_factory() as session:
            suggestion = (
                await session.execute(
                    select(AgentSuggestion).where(AgentSuggestion.run_id == run.id)
                )
            ).scalar_one()
        assert suggestion.suggestion_type == summary.SUGGESTION_TYPE == "summary"
        assert suggestion.prompt_version == summary.PROMPT_VERSION == "summary_agent.v1"
        content = suggestion.content
        for key in ("progress", "completed", "pending_approvals", "risks"):
            assert key in content

        # 模型上下文中的状态统计必须来自数据库快照。
        prompt_sent = provider.calls[0]["prompt"]
        for status in ("COMPLETED", "IN_REVIEW", "BLOCKED", "READY"):
            assert f'"{status}": 1' in prompt_sent
        assert "已完成事项" in prompt_sent and "待审工作项" in prompt_sent

        assert set(suggestion.fact_refs["work_item_ids"]) == {
            str(completed.id),
            str(in_review.id),
        }
        assert suggestion.fact_refs["transfer_request_ids"] == [str(transfer_id)]
    finally:
        await redis_client.aclose()


async def test_project_level_agent_analysis_api(
    client: httpx.AsyncClient, project: Project
) -> None:
    """项目级分析仅允许负责人触发，并校验 Agent 类型和工作项。"""
    _, leader = await add_member(project, "leader", LEADER_PW, role="leader", display_name="负责人")
    _, alice = await add_member(project, "alice", ALICE_PW, display_name="爱丽丝")
    leader_headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id))
    alice_headers = await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id))

    redis_client = create_redis_client()
    try:
        await redis_client.delete(QUEUE_KEY)

        resp = await client.post(
            "/api/v1/agent-analysis",
            json={"agent_type": "workflow_risk"},
            headers=leader_headers,
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["agent_type"] == "workflow_risk"
        assert body["work_item_id"] is None
        assert body["trigger_source"] == "manual"

        # 可选工作项必须存在于当前项目。
        item = await _make_work_item(leader.id, "关联工作项", project_id=project.id)
        resp = await client.post(
            "/api/v1/agent-analysis",
            json={"agent_type": "summary_agent", "work_item_id": str(item.id)},
            headers=leader_headers,
        )
        assert resp.status_code == 202, resp.text
        assert resp.json()["work_item_id"] == str(item.id)

        resp = await client.post(
            "/api/v1/agent-analysis",
            json={"agent_type": "workflow_risk"},
            headers=alice_headers,
        )
        assert resp.status_code == 403, resp.text

        resp = await client.post(
            "/api/v1/agent-analysis",
            json={"agent_type": "not_registered"},
            headers=leader_headers,
        )
        assert resp.status_code == 400, resp.text

        resp = await client.post(
            "/api/v1/agent-analysis",
            json={"agent_type": "workflow_risk", "work_item_id": str(uuid.uuid4())},
            headers=leader_headers,
        )
        assert resp.status_code == 404, resp.text
    finally:
        await redis_client.aclose()


async def test_deliverable_metadata_tool_exposes_text_and_file_meta_only(
    project: Project, leader: ProjectMember
) -> None:
    """文本交付物可返回正文，文件交付物只能返回元数据。"""
    item = await _make_work_item(leader.id, "混合交付工作项", project_id=project.id, status="IN_PROGRESS")
    async with async_session_factory() as session:
        stored = StoredFile(
            project_id=item.project_id,
            storage_key="2026/07/abc.pdf",
            original_filename="设计文档.pdf",
            size_bytes=12345,
            mime_type="application/pdf",
            sha256="a" * 64,
            uploaded_by=leader.id,
            work_item_id=item.id,
        )
        session.add(stored)
        await session.flush()
        session.add_all(
            [
                Deliverable(
                    # 交付物项目归属与所属工作项保持一致。
                    project_id=project.id,
                    work_item_id=item.id,
                    type="text",
                    content="阶段性实现说明",
                    version=1,
                    submitted_by=leader.id,
                ),
                Deliverable(
                    project_id=project.id,
                    work_item_id=item.id,
                    type="file",
                    stored_file_id=stored.id,
                    version=2,
                    submitted_by=leader.id,
                ),
            ]
        )
        await session.commit()

    async with async_session_factory() as session:
        rows = await TOOL_REGISTRY["list_deliverable_metadata"].func(
            session, item.id, project_id=project.id
        )

    assert [r["version"] for r in rows] == [1, 2]
    assert rows[0]["content"] == "阶段性实现说明"
    assert rows[0]["file"] is None
    assert rows[1]["content"] is None  # 文件正文不得进入 Agent 上下文。
    assert rows[1]["file"] == {
        "original_filename": "设计文档.pdf",
        "size_bytes": 12345,
        "mime_type": "application/pdf",
        "sha256": "a" * 64,
    }
