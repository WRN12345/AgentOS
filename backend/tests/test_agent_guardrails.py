"""T5.3 验收：统一输出 Schema、结构校验诊断与权限护栏（10.2、10.3、17.3 节）。

覆盖：
- Schema 单元契约：合法输出通过；非法 JSON / 缺字段 / 置信度越界抛
  SuggestionValidationError 且诊断信息含 run_id / stage / errors / 原始输出截断；
- 工具注册表护栏（第 22 章标准 10）：注册表无业务写命令、与 10.3 节禁止
  清单不相交、tools 模块源码不 import 任何 domain 写服务、read_query
  工具实现无写调用；
- 端到端：模拟模型返回非法 JSON / 缺字段 → run=failed + 诊断落
  agent_runs.error，不产生正式建议、不发通知（17.3 节）；
- 审计护栏：一次成功 agent run 不产生任何业务状态类审计事件。
"""

import inspect
import json
import uuid

import pytest
from sqlalchemy import func, select

from app.agents import tools as agent_tools
from app.agents.graphs import base as graph_base
from app.agents.models import AgentRun, AgentSuggestion
from app.agents.schemas.suggestion import (
    SuggestionValidationError,
    parse_suggestion_output,
)
from app.agents.service import request_agent_analysis
from app.domains.audit.models import AuditEvent
from app.domains.notifications.models import Notification
from app.domains.project.models import Project, ProjectMember
from app.infrastructure.cache.redis import create_redis_client
from app.infrastructure.database.engine import async_session_factory
from app.workers.worker import handle_task

#: 业务状态类审计 action 前缀（现有命名风格 <domain>.<verb>）；
#: agent 自身事件若出现应为 agent.*，绝不允许这些业务前缀。
#: 含记忆模块写操作（16.10 纳入审计域）：核心记忆与成员档案。
BUSINESS_AUDIT_PREFIXES = (
    "work_item.",
    "transfer.",
    "deadline_change.",
    "deliverable.",
    "review.",
    "collaboration.",
    "file.",
    "member.",
    "member_profile.",
    "core_memory.",
)

#: 允许在 agent run 内产生的审计动作：提议不是业务状态变更——
#: 核心记忆在负责人确认前不变（M6.7，设计文档第 8 节红线）
ALLOWED_AGENT_AUDIT_ACTIONS = ("core_memory.proposed",)

#: 记忆模块新增的只读检索工具（M6.1/M6.2）：护栏断言其不得沦为写通道
MEMORY_TOOL_NAMES = (
    "search_project_documents",
    "search_history_records",
    "get_member_stats",
    "search_member_profiles",
)


def _valid_output() -> dict:
    return {
        "suggestion_type": "echo",
        "content": {"summary": "结论", "rationale": "理由"},
        "fact_refs": {"work_item_ids": [str(uuid.uuid4())]},
        "confidence": 0.8,
        "risks": "占位",
        "prompt_version": "echo.v1",
    }


async def _run_agent_once(redis_client, run_id: uuid.UUID, prompt: str = "") -> None:
    """直接调用 worker 处理函数执行一次 agent.run（不真起进程）。"""
    await handle_task(
        {
            "id": str(uuid.uuid4()),
            "type": "agent.run",
            "payload": {"run_id": str(run_id), "prompt": prompt},
        },
        redis_client,
    )


# ---------- Schema 单元契约 ----------


def test_schema_accepts_valid_output() -> None:
    output = parse_suggestion_output(_valid_output(), run_id="run-1")
    assert output.suggestion_type == "echo"
    assert output.content.summary == "结论"
    assert output.confidence == 0.8


def test_schema_accepts_json_string_and_keeps_extra_content_keys() -> None:
    """模型返回的 JSON 字符串可解析；content 允许能力自有扩展字段（extra）。"""
    payload = _valid_output()
    payload["content"]["candidates"] = [{"member_id": "m1", "reason": "r"}]
    output = parse_suggestion_output(json.dumps(payload), run_id="run-1")
    dumped = output.model_dump(mode="json")
    assert dumped["content"]["candidates"] == [{"member_id": "m1", "reason": "r"}]


def test_schema_rejects_invalid_json() -> None:
    with pytest.raises(SuggestionValidationError) as exc_info:
        parse_suggestion_output("{not json", run_id="run-1")
    diagnostics = exc_info.value.diagnostics
    assert diagnostics["run_id"] == "run-1"
    assert diagnostics["stage"] == "json_parse"
    assert diagnostics["errors"]
    assert diagnostics["raw_output"] == "{not json"


def test_schema_rejects_missing_fields_and_bad_confidence() -> None:
    with pytest.raises(SuggestionValidationError) as exc_info:
        parse_suggestion_output({"suggestion_type": "echo"}, run_id="run-2")
    assert exc_info.value.diagnostics["stage"] == "schema_validate"

    payload = _valid_output()
    payload["confidence"] = 1.5
    with pytest.raises(SuggestionValidationError):
        parse_suggestion_output(payload, run_id="run-3")


# ---------- 工具注册表护栏（10.3 节，第 22 章标准 10） ----------


def test_tool_registry_has_no_business_write_commands() -> None:
    """遍历注册表：只有 read_query / write_suggestion 两类，且无禁止操作。"""
    assert agent_tools.TOOL_REGISTRY, "工具注册表不应为空"
    for name, tool in agent_tools.TOOL_REGISTRY.items():
        assert name == tool.name
        assert tool.kind in ("read_query", "write_suggestion")
        assert callable(tool.func)
    # 写工具只有「写入建议」一个，且只写 agent_suggestions
    write_tools = [t for t in agent_tools.TOOL_REGISTRY.values() if t.kind != "read_query"]
    assert [t.name for t in write_tools] == ["write_suggestion"]
    # 10.3 节禁止操作一项都不允许出现在注册表中
    forbidden_names = {op["operation"] for op in agent_tools.FORBIDDEN_OPERATIONS}
    assert len(forbidden_names) == 7
    assert forbidden_names.isdisjoint(agent_tools.TOOL_REGISTRY)


def test_tools_module_imports_no_domain_write_services() -> None:
    """源码级断言：tools.py 只 import domain models/只读常量，不 import 写服务。"""
    source = inspect.getsource(agent_tools)
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("from app.domains", "import app.domains")):
            assert ".service" not in stripped, f"agent 工具不得 import domain 写服务: {stripped}"


def test_read_query_tools_have_no_write_calls() -> None:
    """源码级断言：read_query 工具实现不含 session.add / session.delete 等写调用。"""
    for tool in agent_tools.TOOL_REGISTRY.values():
        if tool.kind != "read_query":
            continue
        func_source = inspect.getsource(tool.func)
        for write_call in ("session.add", "session.delete", ".execute(insert", ".execute(update"):
            assert write_call not in func_source, f"{tool.name} 含写调用: {write_call}"


# ---------- 端到端：非法输出 → 诊断落库，无正式建议、无通知（17.3 节） ----------


async def _assert_validation_failure(run_id: uuid.UUID, expected_stage: str) -> None:
    async with async_session_factory() as session:
        final = await session.get(AgentRun, run_id)
        assert final is not None
        assert final.status == "failed"
        assert final.error is not None
        # error 形如 "SuggestionValidationError: {诊断 JSON}"（worker 通用失败处理）
        diagnostics = json.loads(final.error.split(": ", 1)[1])
        assert diagnostics["run_id"] == str(run_id)
        assert diagnostics["stage"] == expected_stage
        assert diagnostics["errors"]
        assert "raw_output" in diagnostics

        suggestion_count = (
            await session.execute(select(func.count()).select_from(AgentSuggestion))
        ).scalar_one()
        assert suggestion_count == 0
        notification_count = (
            await session.execute(
                select(func.count())
                .select_from(Notification)
                .where(Notification.type == "agent.suggestion_ready")
            )
        ).scalar_one()
        assert notification_count == 0


async def test_invalid_json_output_saves_diagnostics(
    project: Project, leader: ProjectMember, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型返回非法 JSON → run=failed + 诊断信息，无正式建议、无通知。"""
    monkeypatch.setitem(graph_base.CAPABILITIES, "echo", lambda state: "{not json")

    redis_client = create_redis_client()
    try:
        async with async_session_factory() as session:
            run = await request_agent_analysis(
                session,
                redis_client,
                project_id=project.id,
                agent_type="echo",
                prompt="触发非法 JSON 路径",
            )
        await _run_agent_once(redis_client, run.id)
        await _assert_validation_failure(run.id, "json_parse")
    finally:
        await redis_client.aclose()


async def test_missing_fields_output_saves_diagnostics(
    project: Project, leader: ProjectMember, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型输出缺字段 → run=failed + schema_validate 诊断，无正式建议、无通知。"""
    monkeypatch.setitem(
        graph_base.CAPABILITIES, "echo", lambda state: {"suggestion_type": "echo"}
    )

    redis_client = create_redis_client()
    try:
        async with async_session_factory() as session:
            run = await request_agent_analysis(
                session,
                redis_client,
                project_id=project.id,
                agent_type="echo",
                prompt="触发缺字段路径",
            )
        await _run_agent_once(redis_client, run.id)
        await _assert_validation_failure(run.id, "schema_validate")
    finally:
        await redis_client.aclose()


# ---------- 审计护栏：agent run 不产生业务状态类审计事件 ----------


async def test_agent_run_emits_no_business_audit_events(
    project: Project, leader: ProjectMember
) -> None:
    """跑一个成功的 echo run，断言 audit_events 无业务状态变更类事件。"""
    redis_client = create_redis_client()
    try:
        async with async_session_factory() as session:
            run = await request_agent_analysis(
                session,
                redis_client,
                project_id=project.id,
                agent_type="echo",
                prompt="审计护栏验证",
            )
        await _run_agent_once(redis_client, run.id)

        async with async_session_factory() as session:
            final = await session.get(AgentRun, run.id)
            assert final is not None and final.status == "succeeded"
            events = list((await session.execute(select(AuditEvent))).scalars().all())
        business_events = [
            e for e in events if e.action.startswith(BUSINESS_AUDIT_PREFIXES)
        ]
        assert business_events == [], f"agent 运行产生业务审计事件: {business_events}"
    finally:
        await redis_client.aclose()


# ---------- M6.3 护栏扩展：记忆工具只读 + 提议记忆走建议通道 ----------


def test_memory_tools_are_read_query_only() -> None:
    """记忆检索工具全部注册为 read_query；写工具仍只有 write_suggestion 一个。"""
    for name in MEMORY_TOOL_NAMES:
        tool = agent_tools.TOOL_REGISTRY.get(name)
        assert tool is not None, f"缺少工具 {name}"
        assert tool.kind == "read_query", f"{name} 必须为只读工具"
    write_tools = [t for t in agent_tools.TOOL_REGISTRY.values() if t.kind != "read_query"]
    assert [t.name for t in write_tools] == ["write_suggestion"]


async def test_memory_proposal_via_suggestion_channel_is_not_business_write(
    project: Project, leader: ProjectMember, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"提议记忆条目"走建议写入通道（write_suggestion），不算业务写（M6.3）：

    Agent 产出 memory_proposal 建议后——建议落 agent_suggestions、不产生任何
    业务状态类审计事件（含 core_memory./member_profile.）、核心记忆表无变化
    （未确认前红线不变，设计文档第 8 节）。
    """
    from app.domains.memory.models import CoreMemoryEntry

    monkeypatch.setitem(
        graph_base.CAPABILITIES,
        "echo",
        lambda state: {
            "suggestion_type": "memory_proposal",
            "content": {
                "summary": "建议记住：改 X 表要同步改 Y",
                "rationale": "历史上漏同步导致过线上故障",
                "action": "create",
                "content": "改 X 表结构一定要同步改 Y",
            },
            "confidence": 0.8,
            "risks": "无",
            "prompt_version": "echo.v1",
        },
    )

    redis_client = create_redis_client()
    try:
        async with async_session_factory() as session:
            run = await request_agent_analysis(
                session,
                redis_client,
                project_id=project.id,
                agent_type="echo",
                prompt="触发记忆提议路径",
            )
        await _run_agent_once(redis_client, run.id)

        async with async_session_factory() as session:
            final = await session.get(AgentRun, run.id)
            assert final is not None and final.status == "succeeded"
            suggestion = (
                await session.execute(
                    select(AgentSuggestion).where(AgentSuggestion.run_id == run.id)
                )
            ).scalar_one()
            assert suggestion.suggestion_type == "memory_proposal"

            events = list((await session.execute(select(AuditEvent))).scalars().all())
            business_events = [
                e
                for e in events
                if e.action.startswith(BUSINESS_AUDIT_PREFIXES)
                and e.action not in ALLOWED_AGENT_AUDIT_ACTIONS
            ]
            assert business_events == [], f"agent 运行产生业务审计事件: {business_events}"

            entries = list((await session.execute(select(CoreMemoryEntry))).scalars().all())
            assert entries == [], "未确认前核心记忆不得变化"
    finally:
        await redis_client.aclose()
