"""Requirement Pipeline：需求 → 拆解 → 分配一体化流水线（设计文档 2026-07-30 §4.1）。

不是第四套独立逻辑，而是在同一个 run 内顺序编排三段模型调用：
1. 需求分析（复用 requirement_analyst 的输出结构 + involved_aspects，
   取值限于 member_capabilities.tag 去重词表）；
2. 拆解（复用 planning_advisor 的输出结构：work_item_breakdown[] +
   collaboration_points[]）；
3. 分配（复用 assignment_advisor 的成员能力/负载只读数据查询），为每个拆解项
   产出 recommended_assignee + candidates + 理由。
4. 记忆评估（M6.7，记忆模块设计文档第 8 节）：判断本次过程是否有值得沉淀的
   约定/决策/教训，产出 memory_proposal 走负责人确认通道（不直接生效）；
   容量快满时优先整合精简提议。

指定人选处理（§3）：需求文本中点名的人选按 display_name/username 匹配
ProjectMember（排除 role=admin 与 is_active=false），作为 hard constraint
传入分配段；系统侧权威标记 user_specified=true（Agent 不得更改指定），合理性
提示只落在该成员的 reason/notes；匹配不到的名字列入 unresolved_mentions[]。

任一段模型输出不是合法 JSON 对象时，先带解析错误反馈重试一次（模型偶发
输出非法 JSON，如同类对象缺括号）；重试后仍非法才原样透传，由
validate_output 产生 json_parse / schema_validate 诊断（与 build_output
同一语义，17.3 节）。
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

if TYPE_CHECKING:  # 避免与 graphs.base 循环导入（base 注册本能力）
    from app.agents.graphs.base import AgentGraphState

AGENT_TYPE = "requirement_pipeline"
SUGGESTION_TYPE = "pipeline"
PROMPT_VERSION = "requirement_pipeline.v1"

#: 需求文本中点名人选的提示语（如"接口部分给张三""测试由李四负责"）；
#: 词表匹配之外的点名进入 unresolved_mentions。
_MENTION_RE = re.compile(
    r"(?:给|交给|指派|派给|由)\s*([A-Za-z0-9_一-鿿]{1,16}?)(?=\s*负责|[，。,；;：:\s]|$)"
)


def resolve_specified_assignees(
    requirement: str, assignable: list[dict]
) -> tuple[list[dict], list[str]]:
    """解析需求文本中点名的人选 →（指定成员列表，未匹配名字列表）。

    - 成员的 display_name 或 username 出现在文本中即视为指定（仅覆盖可分配
      成员：管理员/停用成员已在工具查询层排除，永远不会被指定）；
    - 点名提示语（给/由…负责等）后跟随、但匹配不到任何成员的名字进入
      unresolved_mentions，供表单醒目标出。
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


#: 每段模型调用的最大尝试次数：模型偶发输出非法 JSON（如同类对象缺括号），
#: 带解析错误反馈重试一次通常可恢复；仍失败则透传原文走 json_parse 诊断。
_STAGE_MAX_ATTEMPTS = 2


async def _call_stage_json(*, system: str, user_prompt: str) -> str:
    """调用一段模型并要求合法 JSON 对象；失败时带错误反馈重试，最终返回原文。

    返回值语义与 call_model_json 一致：合法 JSON 对象文本，或（重试后仍
    非法时）最后一次的原始输出，由调用方透传给 validate_output 诊断。
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
    """记忆评估段的产出落为 memory_proposal（M4.4 通道，确认前核心记忆不变）。

    模型输出不可信：动作非 create/consolidate、entry_ids 非法或负载校验失败
    只记日志跳过，绝不影响主建议（M6.3 护栏：提议不产生业务状态写入）。
    """
    action = memory_stage.get("action")
    if action not in ("create", "consolidate"):
        return  # none 或其他：无提议
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
        # M6.4：核心记忆全量常驻注入（第 11 节）；读取失败降级为空（16.5，M6.6 标注）
        core_memory, core_ok = await safe_core_memory_block(
            session, project_id=project_id
        )
        # M6.5：按需检索（文档+历史 ≤8 段/3000 字符）；失败降级为空（16.5）
        requirement = state.get("prompt", "")
        reference, retrieval_ok = await collect_retrieval_block(
            session, project_id=project_id, query=requirement
        )
        # M6.5：分配环节的团队事实记录（完成统计 + 成员档案摘录）
        team_memory, team_ok = await collect_team_memory_block(
            session, project_id=project_id, query=requirement
        )
        # 任一记忆读取失败即降级标注（M6.6 消费此标记）
        memory_ok = core_ok and retrieval_ok and team_ok

    context = state.get("context", {})
    project_name = (context.get("project") or {}).get("name") or ""
    specified, unresolved = resolve_specified_assignees(requirement, assignable)

    # 1. 需求分析：目标/约束/交付物/验收标准 + involved_aspects（限词表取值）
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
        return raw_analysis  # 透传：validate_output 抛 json_parse 诊断

    # 2. 拆解：work_item_breakdown[] + collaboration_points[]
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

    # 3. 分配：复用 assignment 的成员能力/负载数据，指定人选为硬约束
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

    # 4. 记忆评估（M6.7，第 8 节）：值得记住 → memory_proposal（确认后才生效，
    # 核心记忆不变）；容量快满（M4.6 判断）时提示模型优先整合精简
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

    # 合并：assignments 与拆解项按下标一一对应；user_specified 由系统侧按
    # 点名解析结果权威标记，不信任模型自报值（Agent 不得更改用户指定）。
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
    # 16.5 降级标注（M6.6）：任一记忆读取失败（embedding/检索/核心记忆）即退化为
    # 无记忆模式，结果显式标注"本次未参考记忆"，拆解/分配主流程不受影响
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
        # 未匹配点名由系统侧解析注入，不信任模型自报值
        "unresolved_mentions": unresolved,
        "risks": stage_risks,
        # 记忆参考状态（16.5）：ok=已参考记忆；degraded=本次未参考记忆
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
