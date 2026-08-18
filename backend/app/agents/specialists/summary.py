"""Summary Agent：项目进展 / 已完成事项 / 待审批 / 风险摘要（10.1 节，T5.5）。

触发方式：人工项目级触发（POST /agent-analysis，agent_type=summary_agent），
支撑日报、阶段总结与负责人汇总（4.2 节；周期触发首版不做，日报由负责人
按需人工触发，避免低频信息持续占用模型调用）。
上下文经工具注册表只读查询加载：get_work_item_status_counts、
list_recently_completed_work_items、list_pending_approvals，
风险输入复用 list_open_work_items（系统侧划分逾期）与 list_blocked_items。
content 平铺 progress / completed / pending_approvals / risks；
fact_refs 引用纳入摘要的真实数据 ID（统计数字来自系统侧查询，非模型自报）。
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.agents.prompts import summary as summary_prompts
from app.agents.specialists.common import build_output, call_model_json, context_project_id
from app.agents.specialists.risk import split_by_due
from app.agents.tools import TOOL_REGISTRY
from app.core.config import settings
from app.infrastructure.database.engine import async_session_factory

if TYPE_CHECKING:  # 避免与 graphs.base 循环导入（base 注册本能力）
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
