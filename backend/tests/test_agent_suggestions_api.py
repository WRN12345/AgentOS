"""Agent 建议查询与反馈接口测试（12.5 节，T5.7）。

覆盖：
- GET /agent-suggestions：登录成员可读；按 suggestion_type / review_status /
  work_item_id 过滤；limit/offset 分页；未登录 401；出参含关联 run 的
  work_item_id 与 model；
- POST /agent-suggestions/{id}/feedback：负责人采纳/忽略 → 200 且
  review_status/reviewed_by/reviewed_at 落库 + agent.suggestion_feedback
  审计事件；非负责人 403；不存在 404；已反馈重复反馈 409；
- GET /agent-runs[/{id}]：成员可读；status 过滤；不存在 404；
- GET /config：返回 llm_provider 与 llm_is_external。
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
    suggestion_type: str,
    work_item_id: str | None = None,
    review_status: str = "pending",
    model: str | None = "qwen2.5:7b",
) -> AgentSuggestion:
    """直接造一条 run + suggestion（绕过 worker/模型，专注接口语义）。"""
    async with async_session_factory() as session:
        run = AgentRun(
            status="succeeded",
            agent_type=f"{suggestion_type}_agent",
            model=model,
            trigger_source="manual",
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


# ---------- GET /agent-suggestions ----------


async def test_member_can_list_suggestions_with_details(
    client: httpx.AsyncClient, project: Project
) -> None:
    """成员可读；出参含关联 run 的 work_item_id/model 与信封字段。"""
    ctx = await _setup(client, project)
    suggestion = await _make_suggestion(
        suggestion_type="requirement", work_item_id=ctx["item_id"]  # type: ignore[arg-type]
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
    """项目级建议（run 无 work_item_id）出参 work_item_id 为 null。"""
    ctx = await _setup(client, project)
    await _make_suggestion(suggestion_type="risk")
    resp = await client.get(
        "/api/v1/agent-suggestions", headers=ctx["leader_headers"]  # type: ignore[arg-type]
    )
    assert resp.status_code == 200
    assert resp.json()[0]["work_item_id"] is None


async def test_list_suggestions_filters(
    client: httpx.AsyncClient, project: Project
) -> None:
    """按 suggestion_type / review_status / work_item_id 过滤。"""
    ctx = await _setup(client, project)
    await _make_suggestion(suggestion_type="requirement", work_item_id=ctx["item_id"])  # type: ignore[arg-type]
    await _make_suggestion(suggestion_type="risk")
    await _make_suggestion(suggestion_type="summary", review_status="accepted")

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
    """limit/offset 分页。"""
    ctx = await _setup(client, project)
    for i in range(3):
        await _make_suggestion(suggestion_type=f"type{i}")
    headers = ctx["leader_headers"]  # type: ignore[assignment]
    page1 = await client.get("/api/v1/agent-suggestions?limit=2", headers=headers)
    assert len(page1.json()) == 2
    page2 = await client.get("/api/v1/agent-suggestions?limit=2&offset=2", headers=headers)
    assert len(page2.json()) == 1
    ids = {s["id"] for s in page1.json() + page2.json()}
    assert len(ids) == 3


async def test_list_suggestions_unauthenticated(client: httpx.AsyncClient) -> None:
    """未登录 → 401。"""
    resp = await client.get("/api/v1/agent-suggestions")
    assert resp.status_code == 401


# ---------- POST /agent-suggestions/{id}/feedback ----------


async def test_leader_feedback_accepted_persisted_with_audit(
    client: httpx.AsyncClient, project: Project
) -> None:
    """负责人采纳 → 200；review_status/reviewed_by/reviewed_at 落库 + 审计事件。"""
    ctx = await _setup(client, project)
    suggestion = await _make_suggestion(suggestion_type="planning")
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
    """忽略反馈 → review_status=ignored。"""
    ctx = await _setup(client, project)
    suggestion = await _make_suggestion(suggestion_type="risk")
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
    """普通成员反馈 → 403。"""
    ctx = await _setup(client, project)
    suggestion = await _make_suggestion(suggestion_type="requirement")
    resp = await client.post(
        f"/api/v1/agent-suggestions/{suggestion.id}/feedback",
        json={"action": "accepted"},
        headers=ctx["alice_headers"],  # type: ignore[arg-type]
    )
    assert resp.status_code == 403


async def test_feedback_not_found(client: httpx.AsyncClient, project: Project) -> None:
    """建议不存在 → 404。"""
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
    """已反馈的建议再次反馈 → 409 AGENT_SUGGESTION_ALREADY_REVIEWED。"""
    ctx = await _setup(client, project)
    suggestion = await _make_suggestion(suggestion_type="review", review_status="ignored")
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
    """非法 action → 422（Literal 校验）。"""
    ctx = await _setup(client, project)
    suggestion = await _make_suggestion(suggestion_type="summary")
    resp = await client.post(
        f"/api/v1/agent-suggestions/{suggestion.id}/feedback",
        json={"action": "maybe"},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    assert resp.status_code == 422


# ---------- GET /agent-runs[/{id}] 与 GET /config ----------


async def test_list_and_get_agent_runs(
    client: httpx.AsyncClient, project: Project
) -> None:
    """运行列表含状态/错误详情；status 过滤；单条 404。"""
    ctx = await _setup(client, project)
    await _make_suggestion(suggestion_type="risk")
    async with async_session_factory() as session:
        failed = AgentRun(
            status="failed",
            agent_type="workflow_risk",
            trigger_source="scheduler",
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
    """GET /config 返回 provider 与是否外部服务（ollama → false，与部署环境无关）。"""
    # 显式固定 provider，避免宿主机 .env（如 openai_compatible）影响断言
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
    """反馈审计 action 为 agent. 前缀，不产生业务状态类审计事件（10.3 节）。"""
    ctx = await _setup(client, project)
    suggestion = await _make_suggestion(suggestion_type="assignment")
    await client.post(
        f"/api/v1/agent-suggestions/{suggestion.id}/feedback",
        json={"action": "accepted"},
        headers=ctx["leader_headers"],  # type: ignore[arg-type]
    )
    async with async_session_factory() as session:
        # 只看反馈针对该建议写入的审计事件（排除 setup 建工作项的 work_item.created）
        actions = [
            e.action
            for e in (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.target_id == suggestion.id)
                )
            ).scalars().all()
        ]
    assert actions == ["agent.suggestion_feedback"]
