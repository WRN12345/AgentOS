"""Deliverable Review Agent：交付物初审，生成负责人审核清单（10.1 节，T5.5）。

触发方式：工作项提交审核（submit → IN_REVIEW）后由 work_items 服务以
trigger_source="event" 投递（尽力而为，失败不影响主流程，17.3 节）；
也支持人工触发（POST /work-items/{id}/agent-analysis）。
最小上下文（16 节）：验收标准 + 最新交付物版本信息——文本正文 / 文件元数据
（不读文件原文）/ Git 链接文本，经 list_deliverable_metadata 只读加载。
content 平铺 checklist（checkpoint/verdict/evidence）；只生成建议，
不触碰 reviews 等正式业务状态（10.3 节）。
"""

import uuid
from typing import TYPE_CHECKING, Any

from app.agents.prompts import review as review_prompts
from app.agents.specialists.common import build_output, call_model_json, context_project_id
from app.agents.tools import TOOL_REGISTRY
from app.infrastructure.database.engine import async_session_factory

if TYPE_CHECKING:  # 避免与 graphs.base 循环导入（base 注册本能力）
    from app.agents.graphs.base import AgentGraphState

AGENT_TYPE = "deliverable_review"
SUGGESTION_TYPE = "review"
PROMPT_VERSION = "deliverable_review.v1"


async def deliverable_review_capability(state: "AgentGraphState") -> Any:
    """按验收标准对最新交付物版本初审，产出负责人审核清单。"""
    assert state.get("work_item_id"), "deliverable_review 需要 work_item_id"
    work_item_id = uuid.UUID(state["work_item_id"])
    project_id = context_project_id(state)

    async with async_session_factory() as session:
        overview = await TOOL_REGISTRY["get_work_item_overview"].func(
            session, work_item_id, project_id=project_id
        )
        deliverables = await TOOL_REGISTRY["list_deliverable_metadata"].func(
            session, work_item_id, project_id=project_id
        )

    latest = max(deliverables, key=lambda d: d["version"]) if deliverables else None
    context = state.get("context", {})
    user_prompt = review_prompts.render_user_prompt(
        project_name=(context.get("project") or {}).get("name") or "",
        work_item=overview,
        acceptance_criteria=(overview or {}).get("acceptance_criteria"),
        latest_deliverable=latest,
    )
    raw = await call_model_json(system=review_prompts.SYSTEM_PROMPT, user_prompt=user_prompt)

    fact_refs: dict[str, list[str]] = {
        "work_item_ids": [str(work_item_id)],
        "deliverable_ids": [d["id"] for d in deliverables],
    }
    return build_output(
        raw,
        suggestion_type=SUGGESTION_TYPE,
        prompt_version=PROMPT_VERSION,
        fact_refs=fact_refs,
    )
