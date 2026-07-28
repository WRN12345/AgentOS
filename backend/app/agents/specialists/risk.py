"""Workflow Risk Agent：识别逾期、阻塞、频繁转派与协作等待风险（10.1 节，T5.5）。

触发方式：scheduler 周期风险扫描（app.workers.risk_scan，4.2 节，
trigger_source="scheduler"）或人工项目级触发（POST /agent-analysis）。
上下文经工具注册表只读查询加载：list_open_work_items（系统侧再按当前时间
划分逾期/临期）、list_blocked_items、list_transfer_history、
list_waiting_collaborations。content 平铺 risks 列表（type/target/severity/
detail）；fact_refs 引用纳入考量的真实工作项/协作请求/转派记录 ID。
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.agents.prompts import risk as risk_prompts
from app.agents.specialists.common import build_output, call_model_json
from app.agents.tools import TOOL_REGISTRY
from app.core.config import settings
from app.infrastructure.database.engine import async_session_factory

if TYPE_CHECKING:  # 避免与 graphs.base 循环导入（base 注册本能力）
    from app.agents.graphs.base import AgentGraphState

AGENT_TYPE = "workflow_risk"
SUGGESTION_TYPE = "risk"
PROMPT_VERSION = "workflow_risk.v1"


def split_by_due(
    open_items: list[dict], *, now: datetime, horizon_hours: int
) -> tuple[list[dict], list[dict]]:
    """把进行中工作项按截止时间划分为（已逾期， 临期）；无 DDL 的不纳入。"""
    horizon = now + timedelta(hours=horizon_hours)
    overdue: list[dict] = []
    due_soon: list[dict] = []
    for row in open_items:
        if not row.get("due_at"):
            continue
        due = datetime.fromisoformat(row["due_at"])
        if due < now:
            overdue.append(row)
        elif due <= horizon:
            due_soon.append(row)
    return overdue, due_soon


async def workflow_risk_capability(state: "AgentGraphState") -> Any:
    """扫描逾期/阻塞/频繁转派/协作等待信号，产出风险建议。"""
    async with async_session_factory() as session:
        open_items = await TOOL_REGISTRY["list_open_work_items"].func(session)
        blocked_items = await TOOL_REGISTRY["list_blocked_items"].func(session)
        transfers = await TOOL_REGISTRY["list_transfer_history"].func(session)
        waiting_collabs = await TOOL_REGISTRY["list_waiting_collaborations"].func(session)

    now = datetime.now(UTC)
    overdue, due_soon = split_by_due(
        open_items, now=now, horizon_hours=settings.due_soon_horizon_hours
    )

    context = state.get("context", {})
    user_prompt = risk_prompts.render_user_prompt(
        project_name=(context.get("project") or {}).get("name") or "",
        now=now.isoformat(),
        overdue_items=overdue,
        due_soon_items=due_soon,
        blocked_items=blocked_items,
        transfer_history=transfers,
        waiting_collaborations=waiting_collabs,
    )
    raw = await call_model_json(system=risk_prompts.SYSTEM_PROMPT, user_prompt=user_prompt)

    fact_refs: dict[str, list[str]] = {
        "work_item_ids": [row["id"] for row in open_items],
        "collaboration_request_ids": [row["id"] for row in waiting_collabs],
        "transfer_request_ids": [row["id"] for row in transfers],
    }
    return build_output(
        raw,
        suggestion_type=SUGGESTION_TYPE,
        prompt_version=PROMPT_VERSION,
        fact_refs=fact_refs,
    )
