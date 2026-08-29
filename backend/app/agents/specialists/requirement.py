"""Requirement Analyst：将自然语言需求整理为目标、约束、交付物和验收标准。"""

from typing import TYPE_CHECKING, Any

from app.agents.prompts import requirement as requirement_prompts
from app.agents.specialists.common import build_output, call_model_json

if TYPE_CHECKING:  # graphs.base 会注册本能力，此处仅在类型检查时导入以避免循环依赖
    from app.agents.graphs.base import AgentGraphState

AGENT_TYPE = "requirement_analyst"
SUGGESTION_TYPE = "requirement"
PROMPT_VERSION = "requirement_analyst.v1"


async def requirement_analyst_capability(state: "AgentGraphState") -> Any:
    """将需求原文整理为结构化建议；模型错误直接交由 worker 标记 run failed。"""
    context = state.get("context", {})
    work_item = context.get("work_item") or {}
    user_prompt = requirement_prompts.render_user_prompt(
        project_name=(context.get("project") or {}).get("name") or "",
        work_item_title=work_item.get("title"),
        requirement=state.get("prompt", ""),
    )
    raw = await call_model_json(
        system=requirement_prompts.SYSTEM_PROMPT, user_prompt=user_prompt
    )
    fact_refs = (
        {"work_item_ids": [state["work_item_id"]]} if state.get("work_item_id") else {}
    )
    return build_output(
        raw,
        suggestion_type=SUGGESTION_TYPE,
        prompt_version=PROMPT_VERSION,
        fact_refs=fact_refs,
    )
