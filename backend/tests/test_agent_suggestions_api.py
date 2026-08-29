"""验证 Agent 建议、运行记录、反馈和模型配置接口。

建议列表应支持项目内过滤和分页；仅负责人可以反馈，且反馈必须持久化审计信息。
运行详情按项目成员身份授权；模型配置向所有已登录用户开放。
"""

import uuid

import httpx
import pytest
from sqlalchemy import select

from app.agents.models import AgentRun, AgentSuggestion
from app.core.config import settings
from app.domains.audit.models import AuditEvent
from app.domains.project.models import Project, ProjectMember
from app.infrastructure.database.engine import async_session_factory
from tests.conftest import add_member, auth_headers

LEADER_PW = "Leader123!"
ALICE_PW = "Alice123!"


async def _setup(client: httpx.AsyncClient, project: Project) -> dict[str, object]:
    _, leader = await add_member(project, "leader", LEADER_PW, role="leader", display_name="负责人")
    _, alice = await add_member(project, "alice", ALICE_PW, display_name="爱丽丝")
    leader_headers = await auth_headers(client, "leader", LEADER_PW, project_id=str(project.id))
    item_resp = await client.post(
        "/api/v1/work-items",
        json={"title": "RAG 工作项", "description": "实现 RAG", "assignee_id": str(alice.id)},
        headers=leader_headers,
    )
    assert item_resp.status_code == 201, item_resp.text
    return {
        "leader": leader,
        "alice": alice,
        "item_id": item_resp.json()["id"],
        "leader_headers": leader_headers,
        "alice_headers": await auth_headers(client, "alice", ALICE_PW, project_id=str(project.id)),
    }


async def _make_suggestion(
    *,
    project_id: uuid.UUID,
    suggestion_type: str,
    work_item_id: str | None = None,
    review_status: str = "pending",
    model: str | None = "qwen2.5:7b",
) -> AgentSuggestion:
    """直接创建带项目归属的运行和建议，以隔离验证接口语义。

    建议通过 ``run.project_id`` 推导项目归属，列表和反馈必须据此隔离。
    """
    async with async_session_factory() as session:
        run = AgentRun(
            status="succeeded",
            agent_type=f"{suggestion_type}_agent",
            model=model,
            trigger_source="manual",
            project_id=project_id,
            work_item_id=uuid.UUID(work_item_id) if work_item_id else None,
        )
        session.add(run)
        await session.flush()
        suggestion = AgentSuggestion(
            run_id=run.id,
            suggestion_type=suggestion_type,
            content={"summary": f"{suggestion_type} 结论", "rationale": "理由"},
            confidence=0.8,
            risks="限制说明",
            fact_refs={"work_item_ids": [work_item_id] if work_item_id else []},
            review_status=review_status,
            prompt_version=f"{suggestion_type}.v1",
        )
        session.add(suggestion)
        await session.commit()
        await session.refresh(suggestion)
        return suggestion


async def test_member_can_list_suggestions_with_details(
    client: httpx.AsyncClient, project: Project
) -> None:
    """项目成员应能读取包含运行信息和信封字段的建议。"""
    ctx = await _setup(client, project)
    suggestion = await _make_suggestion(
        project_id=project.id,
        suggestion_type="requirement",
        work_item_id=ctx["item_id"],  # type: ignore[arg-type]
    )
    resp = await client.get(
        "/api/v1/agent-suggestions", headers=ctx["alice_headers"]  # type: ignore[arg-type]
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert len(items) == 1
    body = items[0]
    assert body["id"] == str(suggestion.id)
    assert body["suggestion_type"] == "requirement"
    assert body["content"]["summary"] == "requirement 结论"
    assert body["confidence"] == 0.8
    assert body["risks"] == "限制说明"
    assert body["fact_refs"]["work_item_ids"] == [ctx["item_id"]]
    assert body["review_status"] == "pending"
    assert body["work_item_id"] == ctx["item_id"]
    assert body["model"] == "qwen2.5:7b"
    assert body["prompt_version"] == "requirement.v1"


async def test_project_level_suggestion_has_null_work_item(
    client: httpx.AsyncClient, project: Project
) -> None:
    """项目级建议没有关联工作项时应返回空 work_item_id。"""
    ctx = await _setup(client, project)
    await _make_suggestion(project_id=project.id, suggestion_type="risk")
    resp = await client.get(
        "/api/v1/agent-suggestions", headers=ctx["leader_headers"]  # type: ignore[arg-type]
    )
    assert resp.status_code == 200
    assert resp.json()[0]["work_item_id"] is None


async def test_list_suggestions_filters(
    client: httpx.AsyncClient, project: Project
) -> None:
    """建议列表应支持按类型、反馈状态和工作项组合过滤。"""
    ctx = await _setup(client, project)
    await _make_suggestion(project_id=project.id, suggestion_type="requirement", work_item_id=ctx["item_id"])  # type: ignore[arg-type]
    await _make_suggestion(project_id=project.id, suggestion_type="risk")
    await _make_suggestion(project_id=project.id, suggestion_type="summary", review_status="accepted")

    headers = ctx["leader_headers"]  # type: ignore[assignment]

    by_type = await client.get(
        "/api/v1/agent-suggestions?suggestion_type=risk", headers=headers
    )
    assert [s["suggestion_type"] for s in by_type.json()] == ["risk"]

    by_status = await client.get(
        "/api/v1/agent-suggestions?review_status=accepted", headers=headers
    )
    assert [s["suggestion_type"] for s in by_status.json()] == ["summary"]

    by_item = await client.get(
        f"/api/v1/agent-suggestions?work_item_id={ctx['item_id']}", headers=headers
    )
    assert [s["suggestion_type"] for s in by_item.json()] == ["requirement"]

    combined = await client.get(
        "/api/v1/agent-suggestions?suggestion_type=requirement&review_status=pending",
        headers=headers,
    )
    assert [s["suggestion_type"] for s in combined.json()] == ["requirement"]

    empty = await client.get(
        f"/api/v1/agent-suggestions?work_item_id={uuid.uuid4()}", headers=headers
    )
    assert empty.json() == []


async def test_list_suggestions_pagination(
    client: httpx.AsyncClient, project: Project
) -> None:
    """建议列表应支持 limit/offset 分页且不遗漏记录。"""
    ctx = await _setup(client, project)
    for i in range(3):
        await _make_suggestion(project_id=project.id, suggestion_type=f"type{i}")
    headers = ctx["leader_headers"]  # type: ignore[assignment]
    page1 = await client.get("/api/v1/agent-suggestions?limit=2", headers=headers)
    assert len(page1.json()) == 2
    page2 = await client.get("/api/v1/agent-suggestions?limit=2&offset=2", headers=headers)
    assert len(page2.json()) == 1
    ids = {s["id"] for s in page1.json() + page2.json()}
    assert len(ids) == 3


async def test_list_suggestions_unauthenticated(client: httpx.AsyncClient) -> None:
    """未登录用户不得读取建议列表。"""
    resp = await client.get("/api/v1/agent-suggestions")
    assert resp.status_code == 401


async def test_leader_feedback_accepted_persisted_with_audit(
    client: httpx.AsyncClient, project: Project
) -> None:
    """负责人采纳建议后应持久化评审人、时间和审计事件。"""
    ctx = await _setup(client, project)
    suggestion = await _make_suggestion(project_id=project.id, suggestion_type="planning")
    leader: ProjectMember = ctx["leader"]  # type: ignore[assignment]

    resp = await client.post(
        f"/api/v1/agent-suggestions/{suggestion.id}/feedback",
        json={"action": "accepted"},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["review_status"] == "accepted"
    assert body["reviewed_by"] == str(leader.id)
    assert body["reviewed_at"] is not None

    async with async_session_factory() as session:
        row = await session.get(AgentSuggestion, suggestion.id)
        assert row is not None
        assert row.review_status == "accepted"
        assert row.reviewed_by == leader.id
        assert row.reviewed_at is not None
        events = list(
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.action == "agent.suggestion_feedback",
                        AuditEvent.target_id == suggestion.id,
                    )
                )
            ).scalars().all()
        )
    assert len(events) == 1
    assert events[0].actor_id == leader.id
    assert events[0].before == {"review_status": "pending"}
    assert events[0].after == {"review_status": "accepted"}


async def test_leader_feedback_ignored(
    client: httpx.AsyncClient, project: Project
) -> None:
    """负责人忽略建议后应保存 ignored 状态。"""
    ctx = await _setup(client, project)
    suggestion = await _make_suggestion(project_id=project.id, suggestion_type="risk")
    resp = await client.post(
        f"/api/v1/agent-suggestions/{suggestion.id}/feedback",
        json={"action": "ignored"},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert resp.status_code == 200
    assert resp.json()["review_status"] == "ignored"


async def test_feedback_non_leader_forbidden(
    client: httpx.AsyncClient, project: Project
) -> None:
    """普通成员不得提交建议反馈。"""
    ctx = await _setup(client, project)
    suggestion = await _make_suggestion(project_id=project.id, suggestion_type="requirement")
    resp = await client.post(
        f"/api/v1/agent-suggestions/{suggestion.id}/feedback",
        json={"action": "accepted"},
        headers=ctx["alice_headers"],  # type: ignore[arg-type]
    )
    assert resp.status_code == 403


async def test_feedback_not_found(client: httpx.AsyncClient, project: Project) -> None:
    """反馈不存在的建议时应返回未找到。"""
    ctx = await _setup(client, project)
    resp = await client.post(
        f"/api/v1/agent-suggestions/{uuid.uuid4()}/feedback",
        json={"action": "accepted"},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


async def test_feedback_duplicate_conflict(
    client: httpx.AsyncClient, project: Project
) -> None:
    """已评审建议不得重复反馈。"""
    ctx = await _setup(client, project)
    suggestion = await _make_suggestion(project_id=project.id, suggestion_type="review", review_status="ignored")
    resp = await client.post(
        f"/api/v1/agent-suggestions/{suggestion.id}/feedback",
        json={"action": "accepted"},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "AGENT_SUGGESTION_ALREADY_REVIEWED"


async def test_feedback_invalid_action_rejected(
    client: httpx.AsyncClient, project: Project
) -> None:
    """反馈 action 不在允许集合时应校验失败。"""
    ctx = await _setup(client, project)
    suggestion = await _make_suggestion(project_id=project.id, suggestion_type="summary")
    resp = await client.post(
        f"/api/v1/agent-suggestions/{suggestion.id}/feedback",
        json={"action": "maybe"},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert resp.status_code == 422


async def test_list_and_get_agent_runs(
    client: httpx.AsyncClient, project: Project
) -> None:
    """运行接口应返回失败详情、支持状态过滤并处理不存在记录。"""
    ctx = await _setup(client, project)
    await _make_suggestion(project_id=project.id, suggestion_type="risk")
    async with async_session_factory() as session:
        failed = AgentRun(
            status="failed",
            agent_type="workflow_risk",
            trigger_source="scheduler",
            project_id=project.id,  # 运行列表必须按项目归属过滤。
            error="ModelUnavailableError: timeout",
            duration_ms=1200,
            retry_count=3,
        )
        session.add(failed)
        await session.commit()
        failed_id = failed.id

    headers = ctx["alice_headers"]  # type: ignore[assignment]
    resp = await client.get("/api/v1/agent-runs?status=failed", headers=headers)
    assert resp.status_code == 200
    runs = resp.json()
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert runs[0]["error"].startswith("ModelUnavailableError")
    assert runs[0]["retry_count"] == 3

    one = await client.get(f"/api/v1/agent-runs/{failed_id}", headers=headers)
    assert one.status_code == 200
    assert one.json()["id"] == str(failed_id)

    missing = await client.get(f"/api/v1/agent-runs/{uuid.uuid4()}", headers=headers)
    assert missing.status_code == 404


async def test_config_exposes_llm_external_flag(
    client: httpx.AsyncClient, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型配置接口应返回 Provider 及其是否为外部服务。"""
    # 固定 Provider，避免宿主机环境变量改变测试语义。
    monkeypatch.setattr(settings, "llm_provider", "ollama")
    ctx = await _setup(client, project)
    resp = await client.get("/api/v1/config", headers=ctx["alice_headers"])  # type: ignore[arg-type]
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["llm_provider"] == "ollama"
    assert body["llm_is_external"] is False

    unauthenticated = await client.get("/api/v1/config")
    assert unauthenticated.status_code == 401


async def test_feedback_does_not_touch_business_state(
    client: httpx.AsyncClient, project: Project
) -> None:
    """建议反馈只能产生 Agent 审计事件，不得记录业务状态变更。"""
    ctx = await _setup(client, project)
    suggestion = await _make_suggestion(project_id=project.id, suggestion_type="assignment")
    await client.post(
        f"/api/v1/agent-suggestions/{suggestion.id}/feedback",
        json={"action": "accepted"},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    async with async_session_factory() as session:
        # 按建议过滤，排除准备数据时产生的工作项审计事件。
        actions = [
            e.action
            for e in (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.target_id == suggestion.id)
                )
            ).scalars().all()
        ]
    assert actions == ["agent.suggestion_feedback"]
