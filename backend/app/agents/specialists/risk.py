"""Workflow Risk Agent：识别逾期、阻塞、频繁转派与协作等待风险。

支持 scheduler 周期扫描和人工项目级触发。上下文全部通过只读工具加载，逾期与临期
由系统按当前时间划分；fact_refs 仅引用实际参与分析的业务记录 ID。
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.agents.prompts import risk as risk_prompts
from app.agents.specialists.common import build_output, call_model_json, context_project_id
from app.agents.tools import TOOL_REGISTRY
from app.core.config import settings
from app.infrastructure.database.engine import async_session_factory

if TYPE_CHECKING:  # graphs.base 会注册本能力，此处仅在类型检查时导入以避免循环依赖
    from app.agents.graphs.base import AgentGraphState

AGENT_TYPE = "workflow_risk"
SUGGESTION_TYPE = "risk"
PROMPT_VERSION = "workflow_risk.v1"


def split_by_due(
    open_items: list[dict], *, now: datetime, horizon_hours: int
) -> tuple[list[dict], list[dict]]:
    """按 DDL 将进行中工作项分为已逾期和临期，无 DDL 的工作项不纳入。"""
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
    project_id = context_project_id(state)
    async with async_session_factory() as session:
        open_items = await TOOL_REGISTRY["list_open_work_items"].func(
            session, project_id=project_id
        )
        blocked_items = await TOOL_REGISTRY["list_blocked_items"].func(
            session, project_id=project_id
        )
        transfers = await TOOL_REGISTRY["list_transfer_history"].func(
            session, project_id=project_id
        )
        waiting_collabs = await TOOL_REGISTRY["list_waiting_collaborations"].func(
            session, project_id=project_id
        )

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
