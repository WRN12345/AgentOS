"""Assignment Advisor：按能力/负载推荐初始负责人及候选人（10.1、6.2 节，T5.4）。

上下文经工具注册表（app.agents.tools）只读查询加载：list_member_capabilities、
get_member_workload、get_work_item_overview。content 自有字段（平铺）：
recommended_assignee / candidates（含理由）/ capability_adjustments
（能力修正建议，仅以建议形式呈现，不自动修改能力或权限，6.2 节）。
fact_refs 引用真实成员与关联工作项 ID。
"""

import uuid
from typing import TYPE_CHECKING, Any

from app.agents.prompts import assignment as assignment_prompts
from app.agents.specialists.common import build_output, call_model_json, context_project_id
from app.agents.tools import TOOL_REGISTRY
from app.infrastructure.database.engine import async_session_factory

if TYPE_CHECKING:  # 避免与 graphs.base 循环导入（base 注册本能力）
    from app.agents.graphs.base import AgentGraphState

AGENT_TYPE = "assignment_advisor"
SUGGESTION_TYPE = "assignment"
PROMPT_VERSION = "assignment_advisor.v1"


async def assignment_advisor_capability(state: "AgentGraphState") -> Any:
    """拉取真实成员能力/负载喂给模型，产出分配建议（含能力修正建议）。"""
    project_id = context_project_id(state)
    async with async_session_factory() as session:
        capabilities = await TOOL_REGISTRY["list_member_capabilities"].func(
            session, project_id=project_id
        )
        workload = await TOOL_REGISTRY["get_member_workload"].func(
            session, project_id=project_id
        )
        overview = None
        if state.get("work_item_id"):
            overview = await TOOL_REGISTRY["get_work_item_overview"].func(
                session, uuid.UUID(state["work_item_id"]), project_id=project_id
            )

    context = state.get("context", {})
    user_prompt = assignment_prompts.render_user_prompt(
        project_name=(context.get("project") or {}).get("name") or "",
        requirement=state.get("prompt", ""),
        work_item_overview=overview,
        capabilities=capabilities,
        workload=workload,
    )
    raw = await call_model_json(
        system=assignment_prompts.SYSTEM_PROMPT, user_prompt=user_prompt
    )

    member_ids = sorted(
        {row["member_id"] for row in capabilities}
        | {row["member_id"] for row in workload}
    )
    fact_refs: dict[str, list[str]] = {"member_ids": member_ids}
    if state.get("work_item_id"):
        fact_refs["work_item_ids"] = [state["work_item_id"]]
    return build_output(
        raw,
        suggestion_type=SUGGESTION_TYPE,
        prompt_version=PROMPT_VERSION,
        fact_refs=fact_refs,
    )
