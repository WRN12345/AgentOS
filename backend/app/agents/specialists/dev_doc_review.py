"""Dev Doc Review Agent：初审开发文档并生成负责人确认清单。

开发文档提交后以 event 方式尽力投递，失败不影响提交主流程；也支持人工触发。
仅通过只读工具加载工作项标题、验收标准和 Markdown 正文。Agent 只生成建议，
确认或打回仍由负责人操作。
"""

import uuid
from typing import TYPE_CHECKING, Any

from app.agents.prompts import dev_doc_review as dev_doc_review_prompts
from app.agents.specialists.common import build_output, call_model_json, context_project_id
from app.agents.tools import TOOL_REGISTRY
from app.infrastructure.database.engine import async_session_factory

if TYPE_CHECKING:  # graphs.base 会注册本能力，此处仅在类型检查时导入以避免循环依赖
    from app.agents.graphs.base import AgentGraphState

AGENT_TYPE = "dev_doc_review"
SUGGESTION_TYPE = "dev_doc_review"
PROMPT_VERSION = "dev_doc_review.v1"


async def dev_doc_review_capability(state: "AgentGraphState") -> Any:
    """对工作项当前开发文档做初审，产出负责人确认清单建议。"""
    assert state.get("work_item_id"), "dev_doc_review 需要 work_item_id"
    work_item_id = uuid.UUID(state["work_item_id"])
    project_id = context_project_id(state)

    async with async_session_factory() as session:
        overview = await TOOL_REGISTRY["get_work_item_overview"].func(
            session, work_item_id, project_id=project_id
        )
        dev_doc = await TOOL_REGISTRY["get_dev_doc"].func(
            session, work_item_id, project_id=project_id
        )

    context = state.get("context", {})
    user_prompt = dev_doc_review_prompts.render_user_prompt(
        project_name=(context.get("project") or {}).get("name") or "",
        work_item=overview,
        dev_doc=dev_doc,
    )
    raw = await call_model_json(
        system=dev_doc_review_prompts.SYSTEM_PROMPT, user_prompt=user_prompt
    )

    fact_refs: dict[str, list[str]] = {"work_item_ids": [str(work_item_id)]}
    if dev_doc is not None:
        fact_refs["dev_doc_ids"] = [dev_doc["id"]]
    return build_output(
        raw,
        suggestion_type=SUGGESTION_TYPE,
        prompt_version=PROMPT_VERSION,
        fact_refs=fact_refs,
    )
