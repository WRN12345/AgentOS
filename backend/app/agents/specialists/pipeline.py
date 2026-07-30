"""Requirement Pipeline：需求 → 拆解 → 分配一体化流水线（设计文档 2026-07-30 §4.1）。

不是第四套独立逻辑，而是在同一个 run 内顺序编排三段模型调用：
1. 需求分析（复用 requirement_analyst 的输出结构 + involved_aspects，
   取值限于 member_capabilities.tag 去重词表）；
2. 拆解（复用 planning_advisor 的输出结构：work_item_breakdown[] +
   collaboration_points[]）；
3. 分配（复用 assignment_advisor 的成员能力/负载只读数据查询），为每个拆解项
   产出 recommended_assignee + candidates + 理由。

指定人选处理（§3）：需求文本中点名的人选按 display_name/username 匹配
ProjectMember（排除 role=admin 与 is_active=false），作为 hard constraint
传入分配段；系统侧权威标记 user_specified=true（Agent 不得更改指定），合理性
提示只落在该成员的 reason/notes；匹配不到的名字列入 unresolved_mentions[]。

任一段模型输出不是合法 JSON 对象时原样透传，由 validate_output 产生
json_parse / schema_validate 诊断（与 build_output 同一语义，17.3 节）。
"""

import json
import re
from typing import TYPE_CHECKING, Any

from app.agents.prompts import pipeline as pipeline_prompts
from app.agents.specialists.common import build_output, call_model_json
from app.agents.tools import TOOL_REGISTRY
from app.infrastructure.database.engine import async_session_factory

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


async def requirement_pipeline_capability(state: "AgentGraphState") -> Any:
    """顺序执行 需求分析 → 拆解 → 分配 三段，合并为一条 pipeline 建议。"""
    async with async_session_factory() as session:
        capability_tags = await TOOL_REGISTRY["list_capability_tags"].func(session)
        capabilities = await TOOL_REGISTRY["list_member_capabilities"].func(session)
        workload = await TOOL_REGISTRY["get_member_workload"].func(session)
        open_work_items = await TOOL_REGISTRY["list_open_work_items"].func(session)
        assignable = await TOOL_REGISTRY["list_assignable_members"].func(session)

    context = state.get("context", {})
    project_name = (context.get("project") or {}).get("name") or ""
    requirement = state.get("prompt", "")
    specified, unresolved = resolve_specified_assignees(requirement, assignable)

    # 1. 需求分析：目标/约束/交付物/验收标准 + involved_aspects（限词表取值）
    raw_analysis = await call_model_json(
        system=pipeline_prompts.ANALYZE_SYSTEM_PROMPT,
        user_prompt=pipeline_prompts.render_analyze_prompt(
            project_name=project_name,
            requirement=requirement,
            capability_tags=capability_tags,
        ),
    )
    analysis = _load_stage(raw_analysis)
    if analysis is None:
        return raw_analysis  # 透传：validate_output 抛 json_parse 诊断

    # 2. 拆解：work_item_breakdown[] + collaboration_points[]
    raw_breakdown = await call_model_json(
        system=pipeline_prompts.BREAKDOWN_SYSTEM_PROMPT,
        user_prompt=pipeline_prompts.render_breakdown_prompt(
            project_name=project_name,
            requirement=requirement,
            analysis=analysis,
            open_work_items=open_work_items,
            workload=workload,
        ),
    )
    breakdown_stage = _load_stage(raw_breakdown)
    if breakdown_stage is None:
        return raw_breakdown
    breakdown = breakdown_stage.get("work_item_breakdown") or []

    # 3. 分配：复用 assignment 的成员能力/负载数据，指定人选为硬约束
    raw_assign = await call_model_json(
        system=pipeline_prompts.ASSIGN_SYSTEM_PROMPT,
        user_prompt=pipeline_prompts.render_assign_prompt(
            project_name=project_name,
            breakdown=breakdown,
            capabilities=capabilities,
            workload=workload,
            specified=specified,
        ),
    )
    assign_stage = _load_stage(raw_assign)
    if assign_stage is None:
        return raw_assign
    assignments = assign_stage.get("assignments") or []

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
