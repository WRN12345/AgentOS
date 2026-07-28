"""Requirement Analyst：自然语言需求 → 目标/约束/交付物/验收标准（10.1 节，T5.4）。

能力函数挂入基础图 CAPABILITIES（app.agents.graphs.base），输出走 T5.3 统一
Schema 校验。content 自有字段（平铺）：goals / constraints / deliverables /
acceptance_criteria（均为字符串数组）。
"""

from typing import TYPE_CHECKING, Any

from app.agents.prompts import requirement as requirement_prompts
from app.agents.specialists.common import build_output, call_model_json

if TYPE_CHECKING:  # 避免与 graphs.base 循环导入（base 注册本能力）
    from app.agents.graphs.base import AgentGraphState

AGENT_TYPE = "requirement_analyst"
SUGGESTION_TYPE = "requirement"
PROMPT_VERSION = "requirement_analyst.v1"


async def requirement_analyst_capability(state: "AgentGraphState") -> Any:
    """整理需求原文为结构化建议（模型不可用错误直接冒泡，run 标 failed）。"""
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
