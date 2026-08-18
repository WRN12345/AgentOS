"""Requirement Pipeline 验收（设计文档 2026-07-30 §4/§7，mock 模型）。

覆盖：
- 输出契约：三段脚本化模型输出 → 建议入库，content 符合 §4.2 结构
  （goals/involved_aspects/work_item_breakdown/recommended_assignee/
  unresolved_mentions 等），prompt_version=requirement_pipeline.v1，
  fact_refs 引用真实成员 ID；
- Schema 单元契约：pipeline 载荷缺字段/空拆解/多余字段抛
  SuggestionValidationError（stage=schema_validate）；
- 指定人选解析：display_name/username 匹配；管理员与停用成员被排除
  （点名后落入 unresolved_mentions，永远不会成为 recommended_assignee）；
  匹配不到的名字进 unresolved_mentions；
- 护栏：pipeline 运行只写 agent_suggestions，不产生业务审计事件、不触碰
  工作项；模型输出非法 JSON → run=failed + 诊断落库，不产生建议。
"""

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select

from app.agents.models import AgentRun, AgentSuggestion
from app.agents.schemas.suggestion import (
    SuggestionValidationError,
    parse_suggestion_output,
)
from app.agents.service import request_agent_analysis
from app.agents.specialists import pipeline
from app.agents.specialists.pipeline import resolve_specified_assignees
from app.core.config import settings
from app.domains.audit.models import AuditEvent
from app.domains.project.models import MemberCapability, Project, ProjectMember
from app.domains.work_items.models import WorkItem
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.workers.worker import handle_task
from tests.conftest import add_member


class _ScriptedProvider:
    """模型替身：按调用顺序依次返回脚本化 JSON 文本，记录每次调用。"""

    name = "scripted"
    model = "scripted-model"
    is_external = False

    def __init__(self, scripts: list[str]):
        self._scripts = list(scripts)
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self, prompt: str, *, system: str | None = None, json_output: bool = False
    ) -> str:
        self.calls.append({"prompt": prompt, "system": system, "json_output": json_output})
        assert self._scripts, "模型调用次数超出脚本长度"
        return self._scripts.pop(0)


def _patch_provider(monkeypatch: pytest.MonkeyPatch, provider: _ScriptedProvider) -> None:
    monkeypatch.setattr(
        "app.agents.specialists.common.get_model_provider", lambda: provider
    )


async def _run_once(redis_client, run_id: uuid.UUID, prompt: str = "") -> None:
    """直接调用 worker 处理函数执行一次 agent.run（不真起进程）。

    worker 从任务 payload 取 prompt 作为图输入（app.workers.agent_run），
    因此需要把需求原文原样透传。
    """
    await handle_task(
        {
            "id": str(uuid.uuid4()),
            "type": "agent.run",
            "payload": {"run_id": str(run_id), "prompt": prompt},
        },
        redis_client,
    )


async def _trigger(redis_client, prompt: str, *, project_id: uuid.UUID) -> AgentRun:
    async with async_session_factory() as session:
        return await request_agent_analysis(
            session,
            redis_client,
            agent_type=pipeline.AGENT_TYPE,
            trigger_source="manual",
            work_item_id=None,  # 项目级触发（仅 leader，端点层权限沿用现有实现）
            project_id=project_id,  # 项目级 run 必须带归属（ticket 05：工具按项目过滤）
            prompt=prompt,
        )


async def _single_suggestion() -> AgentSuggestion:
    async with async_session_factory() as session:
        suggestions = list((await session.execute(select(AgentSuggestion))).scalars().all())
    assert len(suggestions) == 1
    return suggestions[0]


def _analysis_stage() -> str:
    return json.dumps(
        {
            "goals": ["搭建 RAG 问答平台"],
            "constraints": ["两周内上线"],
            "deliverables": ["问答 API", "测试报告"],
            "acceptance_criteria": ["评估集准确率 ≥ 80%"],
            "involved_aspects": ["RAG", "FastAPI"],
        },
        ensure_ascii=False,
    )


def _breakdown_stage() -> str:
    return json.dumps(
        {
            "summary": "建议拆分为接口开发与测试两个工作项",
            "rationale": "接口与测试可先后衔接推进",
            "work_item_breakdown": [
                {
                    "title": "问答接口开发",
                    "description": "实现 RAG 问答 API",
                    "acceptance_criteria": "接口返回检索增强答案",
                    "priority": "P1",
                    "suggested_due_at": "2026-08-10",
                },
                {
                    "title": "问答链路测试",
                    "description": "构建评估集并回归测试",
                    "acceptance_criteria": "评估集准确率 ≥ 80%",
                    "priority": "P2",
                    "suggested_due_at": "2026-08-15",
                },
            ],
            "collaboration_points": ["接口输出格式需与测试评估集对齐"],
            "risks": ["排期紧张"],
            "confidence": 0.8,
        },
        ensure_ascii=False,
    )


def _assign_stage(zhangsan: ProjectMember, lisi: ProjectMember) -> str:
    return json.dumps(
        {
            "assignments": [
                {
                    "recommended_assignee": {
                        "member_id": str(zhangsan.id),
                        "display_name": "张三",
                        "reason": "用户指定且具备 RAG 能力 4 级",
                    },
                    "candidates": [
                        {
                            "member_id": str(lisi.id),
                            "display_name": "李四",
                            "reason": "FastAPI 能力 3 级，可兜底",
                        }
                    ],
                    "notes": "",
                },
                {
                    "recommended_assignee": {
                        "member_id": str(lisi.id),
                        "display_name": "李四",
                        "reason": "用户指定",
                    },
                    "candidates": [],
                    "notes": "李四无测试类能力标签，技能匹配度一般",
                },
            ],
            "risks": ["张三当前有 1 个活跃工作项"],
        },
        ensure_ascii=False,
    )


# ---------- 指定人选解析（纯函数单元测试） ----------


def test_resolve_specified_assignees_matches_display_name_and_username() -> None:
    """display_name 与 username（大小写不敏感）出现在文本中即视为指定。"""
    assignable = [
        {"member_id": "m1", "display_name": "张三", "username": "zhangsan"},
        {"member_id": "m2", "display_name": "李四", "username": "lisi"},
    ]
    specified, unresolved = resolve_specified_assignees(
        "接口部分给张三，联调交给 LISI", assignable
    )
    assert [m["member_id"] for m in specified] == ["m1", "m2"]
    assert unresolved == []


def test_resolve_specified_assignees_collects_unresolved_mentions() -> None:
    """点名提示语后匹配不到成员的名字进 unresolved_mentions；重复名字去重。"""
    assignable = [{"member_id": "m1", "display_name": "张三", "username": "zhangsan"}]
    specified, unresolved = resolve_specified_assignees(
        "接口给张三，部署给赵六，运维由赵六负责", assignable
    )
    assert [m["member_id"] for m in specified] == ["m1"]
    assert unresolved == ["赵六"]


def test_resolve_specified_assignees_never_matches_absent_members() -> None:
    """不在可分配清单中的成员（管理员/停用成员已被工具查询层排除）不会被指定。"""
    assignable = [{"member_id": "m1", "display_name": "张三", "username": "zhangsan"}]
    specified, unresolved = resolve_specified_assignees("审查给王管理", assignable)
    assert specified == []
    assert unresolved == ["王管理"]


# ---------- Schema 单元契约（§4.2 载荷校验） ----------


def _valid_pipeline_output() -> dict:
    return {
        "suggestion_type": "pipeline",
        "content": {
            "summary": "拆分为 1 个工作项",
            "rationale": "需求单一",
            "goals": ["目标"],
            "constraints": [],
            "deliverables": [],
            "acceptance_criteria": [],
            "involved_aspects": ["RAG"],
            "work_item_breakdown": [
                {
                    "title": "问答接口",
                    "description": "实现 API",
                    "acceptance_criteria": "返回答案",
                    "priority": "P1",
                    "suggested_due_at": "2026-08-10",
                    "recommended_assignee": {
                        "member_id": "m1",
                        "display_name": "张三",
                        "reason": "RAG 4 级",
                    },
                    "candidates": [],
                    "user_specified": True,
                    "notes": "",
                }
            ],
            "collaboration_points": [],
            "unresolved_mentions": ["赵六"],
            "risks": [],
        },
        "fact_refs": {"member_ids": ["m1"]},
        "confidence": 0.8,
        "risks": "建议需负责人确认",
        "prompt_version": "requirement_pipeline.v1",
    }


def test_schema_accepts_valid_pipeline_payload() -> None:
    output = parse_suggestion_output(_valid_pipeline_output(), run_id="run-1")
    assert output.suggestion_type == "pipeline"
    assert output.content.model_dump()["work_item_breakdown"][0]["user_specified"] is True


def test_schema_rejects_pipeline_payload_missing_fields() -> None:
    payload = _valid_pipeline_output()
    del payload["content"]["involved_aspects"]
    with pytest.raises(SuggestionValidationError) as exc_info:
        parse_suggestion_output(payload, run_id="run-2")
    assert exc_info.value.diagnostics["stage"] == "schema_validate"
    assert exc_info.value.diagnostics["errors"]


def test_schema_rejects_pipeline_empty_breakdown_and_extra_keys() -> None:
    payload = _valid_pipeline_output()
    payload["content"]["work_item_breakdown"] = []
    with pytest.raises(SuggestionValidationError):
        parse_suggestion_output(payload, run_id="run-3")

    payload = _valid_pipeline_output()
    payload["content"]["hacked_field"] = "不应出现"
    with pytest.raises(SuggestionValidationError):
        parse_suggestion_output(payload, run_id="run-4")


# ---------- 端到端：三段编排 + 指定人选 + 护栏 ----------


async def test_pipeline_produces_contract_suggestion_with_user_specified_assignees(
    project: Project, leader: ProjectMember, monkeypatch: pytest.MonkeyPatch
) -> None:
    """合法三段输出 → 建议入库且符合 §4.2；指定人选 user_specified=true；
    停用成员/未匹配名字落入 unresolved_mentions，不会被推荐。
    （多项目后管理员升级为全局角色，不再以成员身份存在，无需单独排除。）"""
    _, zhangsan = await add_member(project, "zhangsan", "Zhang123!", display_name="张三")
    _, lisi = await add_member(project, "lisi", "Li123!", display_name="李四")
    # 管理员不再以成员身份存在（全局 is_admin），此成员仅用于测试未匹配名字
    await add_member(project, "wangguanli", "Wang123!", display_name="王管理")
    _, laoqian = await add_member(project, "laoqian", "Qian123!", display_name="老钱")
    async with async_session_factory() as session:
        session.add_all(
            [
                MemberCapability(member_id=zhangsan.id, tag="RAG", proficiency=4, confirmed=True),
                MemberCapability(member_id=lisi.id, tag="FastAPI", proficiency=3),
            ]
        )
        member = await session.get(ProjectMember, laoqian.id)
        assert member is not None
        member.is_active = False  # 停用成员不可分配
        # 张三名下 1 个进行中工作项 → 负载/进行中清单数据
        item = WorkItem(title="检索模块", description="描述", project_id=zhangsan.project_id,
                        assignee_id=zhangsan.id, status="IN_PROGRESS")
        item.collaborators = []
        session.add(item)
        await session.commit()

    requirement = (
        "搭建一个 RAG 问答平台，接口部分给张三，测试由李四负责，"
        "部署给赵六，代码审查给王管理，数据迁移给老钱"
    )
    provider = _ScriptedProvider(
        [_analysis_stage(), _breakdown_stage(), _assign_stage(zhangsan, lisi)]
    )
    _patch_provider(monkeypatch, provider)

    async with async_session_factory() as session:
        baseline_audit = set((await session.execute(select(AuditEvent.id))).scalars().all())
        baseline_work_items = (
            await session.execute(select(func.count()).select_from(WorkItem))
        ).scalar_one()

    redis_client = create_redis_client()
    try:
        run = await _trigger(redis_client, requirement, project_id=project.id)
        await _run_once(redis_client, run.id, prompt=requirement)

        async with async_session_factory() as session:
            final = await session.get(AgentRun, run.id)
            assert final is not None and final.status == "succeeded", final.error

        suggestion = await _single_suggestion()
        assert suggestion.run_id == run.id
        assert suggestion.suggestion_type == pipeline.SUGGESTION_TYPE == "pipeline"
        assert suggestion.prompt_version == pipeline.PROMPT_VERSION == "requirement_pipeline.v1"
        content = suggestion.content
        # 需求分析段：involved_aspects 来自成员技能标签词表
        assert content["goals"] == ["搭建 RAG 问答平台"]
        assert content["involved_aspects"] == ["RAG", "FastAPI"]
        # 拆解段
        assert [b["title"] for b in content["work_item_breakdown"]] == [
            "问答接口开发",
            "问答链路测试",
        ]
        assert content["work_item_breakdown"][0]["suggested_due_at"] == "2026-08-10"
        assert content["collaboration_points"] == ["接口输出格式需与测试评估集对齐"]
        # 分配段：指定人选被尊重并权威标记 user_specified，合理性提示落 notes
        first, second = content["work_item_breakdown"]
        assert first["recommended_assignee"]["member_id"] == str(zhangsan.id)
        assert first["user_specified"] is True
        assert first["candidates"][0]["member_id"] == str(lisi.id)
        assert second["recommended_assignee"]["member_id"] == str(lisi.id)
        assert second["user_specified"] is True
        assert "技能" in second["notes"]
        # 未匹配点名：赵六不存在、老钱已停用 → 进 unresolved
        # 王管理是普通成员，可被正确匹配，不进入 unresolved
        assert sorted(content["unresolved_mentions"]) == sorted(["赵六", "老钱"])
        # fact_refs 引用真实成员 ID
        assert str(zhangsan.id) in suggestion.fact_refs["member_ids"]
        assert str(lisi.id) in suggestion.fact_refs["member_ids"]

        # 模型上下文：分析段带技能标签词表；分配段带成员能力数据与指定人选硬约束
        assert len(provider.calls) == 3
        assert all(call["json_output"] is True for call in provider.calls)
        assert '"RAG"' in provider.calls[0]["prompt"]
        assert "搭建一个 RAG 问答平台" in provider.calls[0]["prompt"]
        assert "张三" in provider.calls[2]["prompt"]
        assert '"proficiency": 4' in provider.calls[2]["prompt"]
        assert "硬约束" in provider.calls[2]["prompt"]

        # 护栏：pipeline 运行不产生业务审计事件、不触碰工作项
        async with async_session_factory() as session:
            current_audit = set((await session.execute(select(AuditEvent.id))).scalars().all())
            assert current_audit - baseline_audit == set()
            work_items_count = (
                await session.execute(select(func.count()).select_from(WorkItem))
            ).scalar_one()
            assert work_items_count == baseline_work_items
    finally:
        await redis_client.aclose()


async def test_pipeline_invalid_stage_output_fails_run_with_diagnostics(
    project: Project, leader: ProjectMember, monkeypatch: pytest.MonkeyPatch
) -> None:
    """拆解段返回非法 JSON（重试后仍非法）→ run=failed + json_parse 诊断，不产生正式建议。"""
    provider = _ScriptedProvider([_analysis_stage(), "{not json", "{still not json"])
    _patch_provider(monkeypatch, provider)
    monkeypatch.setattr(settings, "agent_run_max_retries", 0)  # 一次定终态（17.3 节）

    redis_client = create_redis_client()
    try:
        run = await _trigger(redis_client, "搭建 RAG 问答平台", project_id=project.id)
        await _run_once(redis_client, run.id)

        async with async_session_factory() as session:
            final = await session.get(AgentRun, run.id)
            assert final is not None and final.status == "failed"
            assert final.error is not None
            diagnostics = json.loads(final.error.split(": ", 1)[1])
            assert diagnostics["stage"] == "json_parse"
            assert diagnostics["run_id"] == str(run.id)
            suggestion_count = (
                await session.execute(select(func.count()).select_from(AgentSuggestion))
            ).scalar_one()
            assert suggestion_count == 0
    finally:
        await redis_client.aclose()


async def test_pipeline_stage_retry_recovers_from_invalid_json(
    project: Project, leader: ProjectMember, monkeypatch: pytest.MonkeyPatch
) -> None:
    """拆解段首次输出非法 JSON → 带解析错误反馈重试一次 → 恢复成功（17.3 节）。"""
    _, zhangsan = await add_member(project, "zhangsan", "Zhang123!", display_name="张三")
    _, lisi = await add_member(project, "lisi", "Li123!", display_name="李四")
    provider = _ScriptedProvider(
        [
            _analysis_stage(),
            '{"work_item_breakdown": [{"title": "缺括号"',  # 非法 JSON：对象未闭合
            _breakdown_stage(),
            _assign_stage(zhangsan, lisi),
        ]
    )
    _patch_provider(monkeypatch, provider)

    redis_client = create_redis_client()
    try:
        run = await _trigger(redis_client, "搭建 RAG 问答平台", project_id=project.id)
        await _run_once(redis_client, run.id, prompt="搭建 RAG 问答平台")

        async with async_session_factory() as session:
            final = await session.get(AgentRun, run.id)
            assert final is not None and final.status == "succeeded", final.error

        # 共 4 次模型调用：拆解段多了一次重试，且重试提示词带解析错误反馈
        assert len(provider.calls) == 4
        assert "无法解析为合法 JSON" in provider.calls[2]["prompt"]
    finally:
        await redis_client.aclose()


async def test_pipeline_schema_invalid_merge_fails_run(
    project: Project, leader: ProjectMember, monkeypatch: pytest.MonkeyPatch
) -> None:
    """三段均为合法 JSON 但合并结果违反 §4.2 契约（拆解为空）→ schema_validate 失败。"""
    empty_breakdown = json.dumps(
        {
            "summary": "无法拆解",
            "rationale": "需求信息不足",
            "work_item_breakdown": [],
            "collaboration_points": [],
            "risks": ["需求信息不足"],
            "confidence": 0.3,
        },
        ensure_ascii=False,
    )
    provider = _ScriptedProvider(
        [_analysis_stage(), empty_breakdown, json.dumps({"assignments": [], "risks": []})]
    )
    _patch_provider(monkeypatch, provider)
    monkeypatch.setattr(settings, "agent_run_max_retries", 0)  # 一次定终态（17.3 节）

    redis_client = create_redis_client()
    try:
        run = await _trigger(redis_client, "随便做点什么", project_id=project.id)
        await _run_once(redis_client, run.id)

        async with async_session_factory() as session:
            final = await session.get(AgentRun, run.id)
            assert final is not None and final.status == "failed"
            assert final.error is not None
            diagnostics = json.loads(final.error.split(": ", 1)[1])
            assert diagnostics["stage"] == "schema_validate"
            suggestion_count = (
                await session.execute(select(func.count()).select_from(AgentSuggestion))
            ).scalar_one()
            assert suggestion_count == 0
    finally:
        await redis_client.aclose()
