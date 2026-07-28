"""Planning Advisor：建议工作项拆分、协作点、DDL 与潜在风险（10.1 节，T5.4）。

上下文经工具注册表只读查询加载：list_open_work_items、get_member_workload。
content 自有字段（平铺）：work_item_breakdown（含 suggested_due_date 与
collaborator_hint）/ collaboration_points；潜在风险统一放 risks。
fact_refs 引用纳入考量的进行中工作项 ID。
"""

from typing import TYPE_CHECKING, Any

from app.agents.prompts import planning as planning_prompts
from app.agents.specialists.common import build_output, call_model_json
from app.agents.tools import TOOL_REGISTRY
from app.infrastructure.database.engine import async_session_factory

if TYPE_CHECKING:  # 避免与 graphs.base 循环导入（base 注册本能力）
    from app.agents.graphs.base import AgentGraphState

AGENT_TYPE = "planning_advisor"
SUGGESTION_TYPE = "planning"
PROMPT_VERSION = "planning_advisor.v1"


async def planning_advisor_capability(state: "AgentGraphState") -> Any:
    """结合进行中工作项与负载，产出拆分/协作点/DDL 建议。"""
    async with async_session_factory() as session:
        open_work_items = await TOOL_REGISTRY["list_open_work_items"].func(session)
        workload = await TOOL_REGISTRY["get_member_workload"].func(session)

    context = state.get("context", {})
    work_item = context.get("work_item") or {}
    user_prompt = planning_prompts.render_user_prompt(
        project_name=(context.get("project") or {}).get("name") or "",
        requirement=state.get("prompt", ""),
        work_item_title=work_item.get("title"),
        open_work_items=open_work_items,
        workload=workload,
    )
    raw = await call_model_json(
        system=planning_prompts.SYSTEM_PROMPT, user_prompt=user_prompt
    )

    fact_refs: dict[str, list[str]] = {
        "work_item_ids": [row["id"] for row in open_work_items]
    }
    return build_output(
        raw,
        suggestion_type=SUGGESTION_TYPE,
        prompt_version=PROMPT_VERSION,
        fact_refs=fact_refs,
    )
