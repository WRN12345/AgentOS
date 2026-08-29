"""Planning Advisor：根据进行中工作项和成员负载建议拆分、协作点、DDL 与风险。

上下文通过只读工具加载，fact_refs 引用实际参与分析的工作项 ID。
"""

from typing import TYPE_CHECKING, Any

from app.agents.prompts import planning as planning_prompts
from app.agents.specialists.common import build_output, call_model_json, context_project_id
from app.agents.tools import TOOL_REGISTRY
from app.infrastructure.database.engine import async_session_factory

if TYPE_CHECKING:  # graphs.base 会注册本能力，此处仅在类型检查时导入以避免循环依赖
    from app.agents.graphs.base import AgentGraphState

AGENT_TYPE = "planning_advisor"
SUGGESTION_TYPE = "planning"
PROMPT_VERSION = "planning_advisor.v1"


async def planning_advisor_capability(state: "AgentGraphState") -> Any:
    """结合进行中工作项与负载，产出拆分/协作点/DDL 建议。"""
    project_id = context_project_id(state)
    async with async_session_factory() as session:
        open_work_items = await TOOL_REGISTRY["list_open_work_items"].func(
            session, project_id=project_id
        )
        workload = await TOOL_REGISTRY["get_member_workload"].func(
            session, project_id=project_id
        )

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
