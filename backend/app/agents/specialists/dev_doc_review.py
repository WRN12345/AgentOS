"""Dev Doc Review Agent：开发文档初审（设计文档 2026-07-30 §4.4）。

触发方式：主执行人提交开发文档（dev_doc submit）后由 dev_docs 服务以
trigger_source="event" 投递（尽力而为，失败不影响主流程，17.3 节）；
也支持人工触发（POST /work-items/{id}/agent-analysis）。
最小上下文（16 节）：工作项标题/验收标准 + 文档 Markdown 正文，经
get_work_item_overview / get_dev_doc 只读工具加载。
content 平铺 checklist（aspect/verdict/note）/ alignment / verdict /
risks[]；只生成建议，确认/打回由负责人在审批中心完成（原则 2、10.3 节）。
"""

import uuid
from typing import TYPE_CHECKING, Any

from app.agents.prompts import dev_doc_review as dev_doc_review_prompts
from app.agents.specialists.common import build_output, call_model_json, context_project_id
from app.agents.tools import TOOL_REGISTRY
from app.infrastructure.database.engine import async_session_factory

if TYPE_CHECKING:  # 避免与 graphs.base 循环导入（base 注册本能力）
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
