"""Summary Agent：汇总项目进展、已完成事项、待审批事项和风险。

由负责人按需人工触发，避免低频信息持续占用模型调用。上下文通过只读工具加载，
统计数字由系统查询产生，fact_refs 引用摘要所依据的真实业务记录 ID。
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.agents.prompts import summary as summary_prompts
from app.agents.specialists.common import build_output, call_model_json, context_project_id
from app.agents.specialists.risk import split_by_due
from app.agents.tools import TOOL_REGISTRY
from app.core.config import settings
from app.infrastructure.database.engine import async_session_factory

if TYPE_CHECKING:  # graphs.base 会注册本能力，此处仅在类型检查时导入以避免循环依赖
    from app.agents.graphs.base import AgentGraphState

AGENT_TYPE = "summary_agent"
SUGGESTION_TYPE = "summary"
PROMPT_VERSION = "summary_agent.v1"


async def summary_agent_capability(state: "AgentGraphState") -> Any:
    """汇总真实统计数据，产出项目进展摘要。"""
    project_id = context_project_id(state)
    async with async_session_factory() as session:
        status_counts = await TOOL_REGISTRY["get_work_item_status_counts"].func(
            session, project_id=project_id
        )
        completed = await TOOL_REGISTRY["list_recently_completed_work_items"].func(
            session, project_id=project_id
        )
        pending = await TOOL_REGISTRY["list_pending_approvals"].func(
            session, project_id=project_id
        )
        open_items = await TOOL_REGISTRY["list_open_work_items"].func(
            session, project_id=project_id
        )
        blocked_items = await TOOL_REGISTRY["list_blocked_items"].func(
            session, project_id=project_id
        )

    now = datetime.now(UTC)
    overdue, _ = split_by_due(
        open_items, now=now, horizon_hours=settings.due_soon_horizon_hours
    )

    context = state.get("context", {})
    user_prompt = summary_prompts.render_user_prompt(
        project_name=(context.get("project") or {}).get("name") or "",
        now=now.isoformat(),
        status_counts=status_counts,
        completed_items=completed,
        pending_approvals=pending,
        overdue_items=overdue,
        blocked_items=blocked_items,
    )
    raw = await call_model_json(system=summary_prompts.SYSTEM_PROMPT, user_prompt=user_prompt)

    fact_refs: dict[str, list[str]] = {
        "work_item_ids": [row["id"] for row in completed]
        + [row["id"] for row in pending["in_review_work_items"]],
        "transfer_request_ids": [row["id"] for row in pending["pending_transfers"]],
        "deadline_change_request_ids": [
            row["id"] for row in pending["pending_deadline_changes"]
        ],
    }
    return build_output(
        raw,
        suggestion_type=SUGGESTION_TYPE,
        prompt_version=PROMPT_VERSION,
        fact_refs=fact_refs,
    )
