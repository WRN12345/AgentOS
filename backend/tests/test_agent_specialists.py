"""T5.4 验收：需求/分配/规划三个 Agent 能力（10.1 节，mock 模型）。

覆盖：
- Requirement Analyst：mock 模型返回合法 JSON → 建议入库可查，Schema 字段
  完整（content 平铺 goals/constraints/deliverables/acceptance_criteria），
  prompt_version=requirement_analyst.v1；
- Assignment Advisor：构造成员能力/负载数据 → 断言发给模型的提示词包含真实
  成员能力与负载，建议含候选人及理由，fact_refs 引用真实成员 ID，
  capability_adjustments 仅以建议形式存在（不改动 member_capabilities）；
- Planning Advisor：基本契约（拆分/协作点/DDL 建议字段、fact_refs 引用
  进行中工作项）；
- 模型不可用：ModelUnavailableError 冒泡 → run=failed，不产生建议（17.3 节）。
"""

import json
import uuid

import pytest
from sqlalchemy import func, select

from app.agents.models import AgentRun, AgentSuggestion
from app.agents.service import request_agent_analysis
from app.agents.specialists import assignment, planning, requirement
from app.core.config import settings
from app.domains.project.models import MemberCapability, Project, ProjectMember
from app.domains.work_items.models import WorkItem
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.infrastructure.models.errors import ModelUnavailableError
from app.workers.worker import handle_task
from tests.conftest import add_member


class _FakeProvider:
    """模型替身：返回固定 JSON 文本，记录每次调用（断言最小上下文与 json_output）。"""

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


class _DownProvider(_FakeProvider):
    """模型不可用替身：generate 抛 ModelUnavailableError。"""

    def __init__(self) -> None:
        super().__init__("")

    async def generate(self, prompt: str, *, system: str | None = None, json_output: bool = False) -> str:
        raise ModelUnavailableError("model service down")


def _patch_provider(monkeypatch: pytest.MonkeyPatch, provider: _FakeProvider) -> None:
    monkeypatch.setattr(
        "app.agents.specialists.common.get_model_provider", lambda: provider
    )


async def _run_once(redis_client, run_id: uuid.UUID, prompt: str = "") -> None:
    """直接调用 worker 处理函数执行一次 agent.run（不真起进程）。"""
    await handle_task(
        {
            "id": str(uuid.uuid4()),
            "type": "agent.run",
            "payload": {"run_id": str(run_id), "prompt": prompt},
        },
        redis_client,
    )


async def _make_work_item(
    assignee: ProjectMember, title: str, status: str = "READY"
) -> WorkItem:
    async with async_session_factory() as session:
        item = WorkItem(
            title=title,
            description="描述",
            project_id=assignee.project_id,
            assignee_id=assignee.id,
            status=status,
        )
        item.collaborators = []
        session.add(item)
        await session.commit()
        return item


async def _trigger(redis_client, agent_type: str, work_item_id: uuid.UUID, prompt: str) -> AgentRun:
    async with async_session_factory() as session:
        return await request_agent_analysis(
            session,
            redis_client,
            agent_type=agent_type,
            trigger_source="manual",
            work_item_id=work_item_id,
            prompt=prompt,
        )


async def _single_suggestion() -> AgentSuggestion:
    async with async_session_factory() as session:
        suggestions = list((await session.execute(select(AgentSuggestion))).scalars().all())
    assert len(suggestions) == 1
    return suggestions[0]


# ---------- Requirement Analyst ----------


async def test_requirement_analyst_produces_structured_suggestion(
    project: Project, leader: ProjectMember, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mock 模型返回合法 JSON → 建议入库，content 平铺目标/约束/交付物/验收标准。"""
    provider = _FakeProvider(
        json.dumps(
            {
                "content": {
                    "summary": "将登录需求整理为 1 个目标、2 项约束、1 个交付物",
                    "rationale": "需求原文明确了账号密码登录与安全要求",
                    "goals": ["支持账号密码登录"],
                    "constraints": ["密码需加密存储", "接口需限流"],
                    "deliverables": ["登录 API 与单元测试"],
                    "acceptance_criteria": ["正确账号密码可登录并返回令牌"],
                },
                "confidence": 0.85,
                "risks": "需求未说明会话时长，需人工确认",
            },
            ensure_ascii=False,
        )
    )
    _patch_provider(monkeypatch, provider)

    item = await _make_work_item(leader, "实现用户登录")
    redis_client = create_redis_client()
    try:
        run = await _trigger(
            redis_client,
            requirement.AGENT_TYPE,
            item.id,
            "做一个账号密码登录，密码要安全存储",
        )
        await _run_once(redis_client, run.id, prompt="做一个账号密码登录，密码要安全存储")

        async with async_session_factory() as session:
            final = await session.get(AgentRun, run.id)
            assert final is not None and final.status == "succeeded", final.error

        suggestion = await _single_suggestion()
        assert suggestion.run_id == run.id
        assert suggestion.suggestion_type == requirement.SUGGESTION_TYPE
        assert suggestion.prompt_version == requirement.PROMPT_VERSION == "requirement_analyst.v1"
        assert suggestion.confidence == 0.85
        assert suggestion.risks == "需求未说明会话时长，需人工确认"
        content = suggestion.content
        assert content["summary"].startswith("将登录需求整理")
        assert content["rationale"]
        assert content["goals"] == ["支持账号密码登录"]
        assert content["constraints"] == ["密码需加密存储", "接口需限流"]
        assert content["deliverables"] == ["登录 API 与单元测试"]
        assert content["acceptance_criteria"] == ["正确账号密码可登录并返回令牌"]
        assert suggestion.fact_refs == {"work_item_ids": [str(item.id)]}

        # 模型被要求只输出 JSON，且提示词含需求原文与工作项标题（最小上下文）
        assert provider.calls[0]["json_output"] is True
        assert "做一个账号密码登录" in provider.calls[0]["prompt"]
        assert "实现用户登录" in provider.calls[0]["prompt"]
        assert "只输出" in (provider.calls[0]["system"] or "")
    finally:
        await redis_client.aclose()


# ---------- Assignment Advisor ----------


async def test_assignment_advisor_uses_real_capability_and_workload(
    project: Project, leader: ProjectMember, monkeypatch: pytest.MonkeyPatch
) -> None:
    """提示词含真实成员能力/负载；建议含候选人+理由；fact_refs 引用真实成员。"""
    _, alice = await add_member(project, "alice", "Alice123!", display_name="爱丽丝")
    _, bob = await add_member(project, "bob", "Bob123!", display_name="鲍勃")
    async with async_session_factory() as session:
        session.add_all(
            [
                MemberCapability(member_id=alice.id, tag="RAG", proficiency=4, confirmed=True),
                MemberCapability(member_id=bob.id, tag="FastAPI", proficiency=3),
            ]
        )
        await session.commit()
    # alice 名下两个活跃工作项 → 负载数据；目标工作项挂在 leader 名下
    await _make_work_item(alice, "检索模块", status="IN_PROGRESS")
    await _make_work_item(alice, "向量化管道")
    item = await _make_work_item(leader, "RAG 问答工作项")

    provider = _FakeProvider(
        json.dumps(
            {
                "content": {
                    "summary": "推荐爱丽丝为初始负责人",
                    "rationale": "爱丽丝具备已确认的 RAG 能力（4 级）",
                    "recommended_assignee": {
                        "member_id": str(alice.id),
                        "display_name": "爱丽丝",
                        "reason": "RAG 能力 4 级且经负责人确认",
                    },
                    "candidates": [
                        {
                            "member_id": str(bob.id),
                            "display_name": "鲍勃",
                            "reason": "FastAPI 能力 3 级，可承接接口部分",
                        }
                    ],
                    "capability_adjustments": [
                        {
                            "member_id": str(bob.id),
                            "tag": "RAG",
                            "suggested_proficiency": 2,
                            "reason": "参与 RAG 项目后可补充 RAG 能力标签",
                        }
                    ],
                },
                "confidence": 0.7,
                "risks": "爱丽丝当前负载 2 个活跃工作项，需关注排期",
            },
            ensure_ascii=False,
        )
    )
    _patch_provider(monkeypatch, provider)

    redis_client = create_redis_client()
    try:
        run = await _trigger(
            redis_client, assignment.AGENT_TYPE, item.id, "需要一个 RAG 问答功能"
        )
        await _run_once(redis_client, run.id)

        async with async_session_factory() as session:
            final = await session.get(AgentRun, run.id)
            assert final is not None and final.status == "succeeded", final.error

        suggestion = await _single_suggestion()
        assert suggestion.suggestion_type == assignment.SUGGESTION_TYPE
        assert suggestion.prompt_version == assignment.PROMPT_VERSION == "assignment_advisor.v1"
        content = suggestion.content
        assert content["recommended_assignee"]["member_id"] == str(alice.id)
        assert content["recommended_assignee"]["reason"]
        assert content["candidates"][0]["member_id"] == str(bob.id)
        assert content["candidates"][0]["reason"]
        # 能力修正建议仅以建议形式呈现（6.2 节）
        assert content["capability_adjustments"][0]["suggested_proficiency"] == 2

        # fact_refs 引用真实成员能力与负载数据涉及的成员 + 关联工作项
        assert set(suggestion.fact_refs["member_ids"]) == {
            str(leader.id),
            str(alice.id),
            str(bob.id),
        }
        assert suggestion.fact_refs["work_item_ids"] == [str(item.id)]

        # 发给模型的上下文包含真实成员能力与负载数据
        prompt_sent = provider.calls[0]["prompt"]
        assert "爱丽丝" in prompt_sent and "RAG" in prompt_sent
        assert '"proficiency": 4' in prompt_sent
        assert '"active_work_items": 2' in prompt_sent

        # 能力修正建议未触碰 member_capabilities（6.2 节）
        async with async_session_factory() as session:
            bob_caps = list(
                (
                    await session.execute(
                        select(MemberCapability).where(MemberCapability.member_id == bob.id)
                    )
                )
                .scalars()
                .all()
            )
        assert [(c.tag, c.proficiency) for c in bob_caps] == [("FastAPI", 3)]
    finally:
        await redis_client.aclose()


# ---------- Planning Advisor ----------


async def test_planning_advisor_basic_contract(
    project: Project, leader: ProjectMember, monkeypatch: pytest.MonkeyPatch
) -> None:
    """拆分/协作点/DDL 建议字段齐全；fact_refs 引用进行中工作项。"""
    open_item = await _make_work_item(leader, "既有进行中工作项", status="IN_PROGRESS")
    target = await _make_work_item(leader, "RAG 平台搭建")

    provider = _FakeProvider(
        json.dumps(
            {
                "content": {
                    "summary": "建议拆分为 2 个工作项",
                    "rationale": "检索与生成可并行推进",
                    "work_item_breakdown": [
                        {
                            "title": "检索模块",
                            "description": "实现向量检索",
                            "suggested_due_date": "2026-08-10",
                            "collaborator_hint": "需要具备 RAG 能力的成员协作",
                        },
                        {
                            "title": "生成链路",
                            "description": "接入模型生成",
                            "suggested_due_date": "2026-08-20",
                            "collaborator_hint": None,
                        },
                    ],
                    "collaboration_points": ["检索模块输出需与生成链路对齐接口"],
                },
                "confidence": 0.6,
                "risks": "既有进行中工作项可能争抢人力",
            },
            ensure_ascii=False,
        )
    )
    _patch_provider(monkeypatch, provider)

    redis_client = create_redis_client()
    try:
        run = await _trigger(redis_client, planning.AGENT_TYPE, target.id, "搭建 RAG 平台")
        await _run_once(redis_client, run.id)

        async with async_session_factory() as session:
            final = await session.get(AgentRun, run.id)
            assert final is not None and final.status == "succeeded", final.error

        suggestion = await _single_suggestion()
        assert suggestion.suggestion_type == planning.SUGGESTION_TYPE
        assert suggestion.prompt_version == planning.PROMPT_VERSION == "planning_advisor.v1"
        content = suggestion.content
        breakdown = content["work_item_breakdown"]
        assert [b["title"] for b in breakdown] == ["检索模块", "生成链路"]
        assert breakdown[0]["suggested_due_date"] == "2026-08-10"
        assert content["collaboration_points"] == ["检索模块输出需与生成链路对齐接口"]
        assert "争抢人力" in suggestion.risks

        # fact_refs 引用纳入考量的进行中工作项（READY/IN_PROGRESS 均为活跃状态）
        assert set(suggestion.fact_refs["work_item_ids"]) == {
            str(open_item.id),
            str(target.id),
        }
        # 发给模型的上下文含进行中工作项标题
        assert "既有进行中工作项" in provider.calls[0]["prompt"]
    finally:
        await redis_client.aclose()


# ---------- 模型不可用 → 优雅失败（17.3 节） ----------


async def test_model_unavailable_marks_run_failed(
    project: Project, leader: ProjectMember, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型不可用错误冒泡 → run=failed + 错误信息，不产生建议。

    T5.6 起该错误默认按指数退避重试；本用例只验证错误封装与终态记录，
    故把自动重试次数设为 0（退避行为由 test_agent_retry.py 覆盖）。
    """
    monkeypatch.setattr(settings, "agent_run_max_retries", 0)
    _patch_provider(monkeypatch, _DownProvider())

    item = await _make_work_item(leader, "实现用户登录")
    redis_client = create_redis_client()
    try:
        run = await _trigger(redis_client, requirement.AGENT_TYPE, item.id, "需求原文")
        await _run_once(redis_client, run.id)

        async with async_session_factory() as session:
            final = await session.get(AgentRun, run.id)
            assert final is not None
            assert final.status == "failed"
            assert final.error is not None and "ModelUnavailableError" in final.error
            suggestion_count = (
                await session.execute(select(func.count()).select_from(AgentSuggestion))
            ).scalar_one()
            assert suggestion_count == 0
    finally:
        await redis_client.aclose()
