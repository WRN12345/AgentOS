"""Requirement Pipeline：在同一 run 中依次完成需求分析、拆解、分配和记忆评估。

involved_aspects 仅取 member_capabilities.tag 词表值。需求中按 display_name 或
username 点名的可分配成员属于 hard constraint，由系统设置 user_specified；Agent
只能在 reason 或 notes 提示合理性，不能更换人选。未匹配名字进入 unresolved_mentions。

记忆评估只生成待负责人确认的 memory_proposal，不直接修改核心记忆；容量将满时优先
建议整合。每段非法 JSON 会携带解析错误重试一次，仍失败则保留原文供 validate_output
生成 json_parse 或 schema_validate 诊断。
"""

import json
import logging
import re
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.models import AgentRun
from app.agents.prompts import pipeline as pipeline_prompts
from app.agents.specialists.common import build_output, call_model_json, context_project_id
from app.agents.tools import TOOL_REGISTRY
from app.core.errors import ApiException
from app.domains.memory.context import (
    collect_retrieval_block,
    collect_team_memory_block,
    safe_core_memory_block,
)
from app.domains.memory.core_memory import budget_nearly_full, list_entries
from app.domains.memory.proposals import create_memory_proposal
from app.infrastructure.database.engine import async_session_factory

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # graphs.base 会注册本能力，此处仅在类型检查时导入以避免循环依赖
    from app.agents.graphs.base import AgentGraphState

AGENT_TYPE = "requirement_pipeline"
SUGGESTION_TYPE = "pipeline"
PROMPT_VERSION = "requirement_pipeline.v1"

# 匹配“给张三”“由李四负责”等点名表达，词表外名字进入 unresolved_mentions。
_MENTION_RE = re.compile(
    r"(?:给|交给|指派|派给|由)\s*([A-Za-z0-9_一-鿿]{1,16}?)(?=\s*负责|[，。,；;：:\s]|$)"
)


def resolve_specified_assignees(
    requirement: str, assignable: list[dict]
) -> tuple[list[dict], list[str]]:
    """解析需求中点名的人选，返回指定成员和未匹配名字。

    display_name 或 username 出现在文本中即视为指定。管理员和停用成员已由查询层
    排除；点名表达后的未知名字进入 unresolved_mentions，供表单提示。
    """
    text = requirement or ""
    lowered = text.lower()
    specified = [
        member
        for member in assignable
        if member["display_name"] in text or member["username"].lower() in lowered
    ]
    unresolved: list[str] = []
    for token in _MENTION_RE.findall(text):
        matched = any(
            token == member["display_name"] or token.lower() == member["username"].lower()
            for member in assignable
        )
        if not matched and token not in unresolved:
            unresolved.append(token)
    return specified, unresolved


def _load_stage(raw: str) -> dict[str, Any] | None:
    """解析某一段的模型 JSON；非合法 JSON 对象返回 None（由调用方透传原文）。"""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


# 模型偶发产生非法 JSON，附带解析错误重试一次；仍失败则保留原文供诊断。
_STAGE_MAX_ATTEMPTS = 2


async def _call_stage_json(*, system: str, user_prompt: str) -> str:
    """调用单段模型；非法 JSON 会携带解析错误重试，耗尽后返回最后一次原文。

    返回合法 JSON 对象文本，或供 validate_output 诊断的最后一次原始输出。
    """
    prompt = user_prompt
    raw = ""
    for _ in range(_STAGE_MAX_ATTEMPTS):
        raw = await call_model_json(system=system, user_prompt=prompt)
        if _load_stage(raw) is not None:
            break
        error = _stage_parse_error(raw)
        prompt = (
            f"{user_prompt}\n\n【上次输出无法解析为合法 JSON 对象：{error}。"
            "请检查括号与引号配对，仅重新输出一个合法 JSON 对象，不要输出任何其他内容。】"
        )
    return raw


def _stage_parse_error(raw: str) -> str:
    """提取段落输出的 JSON 解析错误描述，用于重试反馈。"""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return str(exc)
    return "输出不是 JSON 对象" if not isinstance(payload, dict) else ""


async def _emit_memory_proposal(
    session: AsyncSession, state: "AgentGraphState", memory_stage: dict[str, Any]
) -> None:
    """将记忆评估结果写为 memory_proposal，确认前不修改核心记忆。

    模型输出不可信：动作不是 create/consolidate、entry_ids 非法或载荷校验失败时，
    仅记录日志并跳过，不能影响主建议或写入业务状态。
    """
    action = memory_stage.get("action")
    if action not in ("create", "consolidate"):
        return  # none 或未知动作均不生成提议
    entry_ids: list[uuid.UUID] | None = None
    if action == "consolidate":
        try:
            entry_ids = [uuid.UUID(str(i)) for i in (memory_stage.get("entry_ids") or [])]
        except (ValueError, TypeError):
            logger.info("memory proposal dropped: invalid entry_ids", exc_info=True)
            return
    try:
        run = await session.get(AgentRun, uuid.UUID(str(state["run_id"])))
    except (ValueError, TypeError):
        return
    if run is None:
        return
    try:
        await create_memory_proposal(
            session,
            run=run,
            action=action,
            content=memory_stage.get("content") or None,
            entry_ids=entry_ids,
            reason=memory_stage.get("reason") or None,
        )
    except ApiException:
        logger.info("memory proposal dropped: invalid payload", exc_info=True)


async def requirement_pipeline_capability(state: "AgentGraphState") -> Any:
    """顺序执行 需求分析 → 拆解 → 分配 三段，合并为一条 pipeline 建议。"""
    project_id = context_project_id(state)
    async with async_session_factory() as session:
        capability_tags = await TOOL_REGISTRY["list_capability_tags"].func(
            session, project_id=project_id
        )
        capabilities = await TOOL_REGISTRY["list_member_capabilities"].func(
            session, project_id=project_id
        )
        workload = await TOOL_REGISTRY["get_member_workload"].func(
            session, project_id=project_id
        )
        open_work_items = await TOOL_REGISTRY["list_open_work_items"].func(
            session, project_id=project_id
        )
        assignable = await TOOL_REGISTRY["list_assignable_members"].func(
            session, project_id=project_id
        )
        # 核心记忆读取失败时降级为空，并在最终结果中标记。
        core_memory, core_ok = await safe_core_memory_block(
            session, project_id=project_id
        )
        # 文档与历史按需检索，失败时降级为空。
        requirement = state.get("prompt", "")
        reference, retrieval_ok = await collect_retrieval_block(
            session, project_id=project_id, query=requirement
        )
        # 分配环节参考团队完成统计和成员档案摘录。
        team_memory, team_ok = await collect_team_memory_block(
            session, project_id=project_id, query=requirement
        )
        # 任一记忆来源读取失败都标记为降级，主流程继续执行。
        memory_ok = core_ok and retrieval_ok and team_ok

    context = state.get("context", {})
    project_name = (context.get("project") or {}).get("name") or ""
    specified, unresolved = resolve_specified_assignees(requirement, assignable)

    # involved_aspects 由提示词约束为技能词表中的值。
    raw_analysis = await _call_stage_json(
        system=pipeline_prompts.ANALYZE_SYSTEM_PROMPT,
        user_prompt=pipeline_prompts.render_analyze_prompt(
            project_name=project_name,
            requirement=requirement,
            capability_tags=capability_tags,
            core_memory=core_memory,
        ),
    )
    analysis = _load_stage(raw_analysis)
    if analysis is None:
        return raw_analysis  # 保留原始输出，交给 validate_output 诊断

    raw_breakdown = await _call_stage_json(
        system=pipeline_prompts.BREAKDOWN_SYSTEM_PROMPT,
        user_prompt=pipeline_prompts.render_breakdown_prompt(
            project_name=project_name,
            requirement=requirement,
            analysis=analysis,
            open_work_items=open_work_items,
            workload=workload,
            core_memory=core_memory,
            reference=reference,
        ),
    )
    breakdown_stage = _load_stage(raw_breakdown)
    if breakdown_stage is None:
        return raw_breakdown
    breakdown = breakdown_stage.get("work_item_breakdown") or []

    # 指定人选是分配阶段不可更改的硬约束。
    raw_assign = await _call_stage_json(
        system=pipeline_prompts.ASSIGN_SYSTEM_PROMPT,
        user_prompt=pipeline_prompts.render_assign_prompt(
            project_name=project_name,
            breakdown=breakdown,
            capabilities=capabilities,
            workload=workload,
            specified=specified,
            core_memory=core_memory,
            team_memory=team_memory,
        ),
    )
    assign_stage = _load_stage(raw_assign)
    if assign_stage is None:
        return raw_assign
    assignments = assign_stage.get("assignments") or []

    # 值得保留的信息只生成 memory_proposal，确认后才生效；容量将满时优先整合。
    if project_id is not None:
        async with async_session_factory() as session:
            entries = await list_entries(session, project_id=project_id)
            active_entries = [e for e in entries if e.status == "active"]
            nearly_full, used_chars, budget_chars = await budget_nearly_full(
                session, project_id=project_id
            )
            raw_memory = await _call_stage_json(
                system=pipeline_prompts.MEMORY_SYSTEM_PROMPT,
                user_prompt=pipeline_prompts.render_memory_prompt(
                    project_name=project_name,
                    requirement=requirement,
                    breakdown_summary=str(breakdown_stage.get("summary") or ""),
                    core_entries=[{"id": str(e.id), "content": e.content} for e in active_entries],
                    used_chars=used_chars,
                    budget_chars=budget_chars,
                    nearly_full=nearly_full,
                ),
            )
            memory_stage = _load_stage(raw_memory)
            if memory_stage is not None:
                await _emit_memory_proposal(session, state, memory_stage)

    # assignments 与拆解项按下标对应；user_specified 以系统解析结果为准。
    specified_ids = {member["member_id"] for member in specified}
    work_items: list[dict[str, Any]] = []
    for index, item in enumerate(breakdown):
        assignment = assignments[index] if index < len(assignments) else {}
        if not isinstance(assignment, dict):
            assignment = {}
        assignee = assignment.get("recommended_assignee")
        user_specified = (
            isinstance(assignee, dict) and assignee.get("member_id") in specified_ids
        )
        work_items.append(
            {
                **item,
                "recommended_assignee": assignee,
                "candidates": assignment.get("candidates") or [],
                "user_specified": user_specified,
                "notes": assignment.get("notes") or "",
            }
        )

    stage_risks = [
        risk
        for risk in (
            *(breakdown_stage.get("risks") or []),
            *(assign_stage.get("risks") or []),
        )
        if isinstance(risk, str)
    ]
    # 任一记忆来源失败即进入无记忆模式并显式标注，但不影响拆解和分配。
    memory_status = "ok" if memory_ok else "degraded"
    if not memory_ok:
        stage_risks.append("本次未参考记忆（记忆/检索服务不可用，已降级为无记忆模式）")
    content = {
        "summary": breakdown_stage.get("summary") or "需求拆解流水线分析完成",
        "rationale": breakdown_stage.get("rationale") or "按需求分析 → 拆解 → 分配顺序编排产出",
        "goals": analysis.get("goals") or [],
        "constraints": analysis.get("constraints") or [],
        "deliverables": analysis.get("deliverables") or [],
        "acceptance_criteria": analysis.get("acceptance_criteria") or [],
        "involved_aspects": analysis.get("involved_aspects") or [],
        "work_item_breakdown": work_items,
        "collaboration_points": breakdown_stage.get("collaboration_points") or [],
        # 未匹配点名以系统解析结果为准。
        "unresolved_mentions": unresolved,
        "risks": stage_risks,
        # ok 表示已参考记忆，degraded 表示本次未参考记忆。
        "memory_status": memory_status,
    }
    member_ids = sorted(
        {row["member_id"] for row in capabilities}
        | {row["member_id"] for row in workload}
        | specified_ids
    )
    fact_refs: dict[str, list[str]] = {
        "member_ids": member_ids,
        "work_item_ids": [row["id"] for row in open_work_items],
    }
    return build_output(
        json.dumps(
            {
                "content": content,
                "confidence": breakdown_stage.get("confidence", 0.5),
                "risks": "；".join(stage_risks) or "拆解与分配仅为建议，需负责人逐项确认",
            },
            ensure_ascii=False,
        ),
        suggestion_type=SUGGESTION_TYPE,
        prompt_version=PROMPT_VERSION,
        fact_refs=fact_refs,
    )
