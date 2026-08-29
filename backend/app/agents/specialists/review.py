"""Deliverable Review Agent：初审交付物并生成负责人审核清单。

工作项进入 IN_REVIEW 后以 event 方式尽力投递，失败不影响提交主流程；也支持人工触发。
仅加载验收标准和最新交付物的必要信息：文本或 Git 链接可读取正文，文件只读取元数据。
输出 checklist 建议，不修改 reviews 等正式业务状态。
"""

import uuid
from typing import TYPE_CHECKING, Any

from app.agents.prompts import review as review_prompts
from app.agents.specialists.common import build_output, call_model_json, context_project_id
from app.agents.tools import TOOL_REGISTRY
from app.infrastructure.database.engine import async_session_factory

if TYPE_CHECKING:  # graphs.base 会注册本能力，此处仅在类型检查时导入以避免循环依赖
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
