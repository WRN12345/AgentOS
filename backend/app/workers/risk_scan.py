"""Workflow Risk Agent 周期风险扫描（4.2 节逾期风险扫描，T5.5）。

由 scheduler 周期 enqueue `agent.risk_scan`、worker 消费执行：
检查无 pending/running 的同类型运行后，调用 request_agent_analysis
（trigger_source="scheduler"）投递一次项目级 workflow_risk 分析；
真正的图运行仍走 `agent.run` 队列，由 worker 统一承载（4.2 节）。

去重：已存在 pending/running 的 workflow_risk run 时跳过本次投递，
避免周期触发在同一时刻重复堆积（T5.6 的重试策略另行接管失败恢复）。
"""

import redis.asyncio as redis
from sqlalchemy import select

from app.agents.models import AgentRun
from app.agents.service import request_agent_analysis
from app.agents.specialists.risk import AGENT_TYPE as RISK_AGENT_TYPE
from app.core.logging import setup_logging
from app.infrastructure.database.engine import async_session_factory

logger = setup_logging("worker")

#: scheduler 投递、worker 消费的任务类型
RISK_SCAN_TASK_TYPE = "agent.risk_scan"


async def run_risk_scan(client: redis.Redis) -> dict[str, str]:
    """执行一轮风险扫描触发：去重后投递 workflow_risk 分析，返回结果标记。"""
    async with async_session_factory() as session:
        active_run = (
            await session.execute(
                select(AgentRun.id)
                .where(
                    AgentRun.agent_type == RISK_AGENT_TYPE,
                    AgentRun.status.in_(("pending", "running")),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if active_run is not None:
            logger.info("risk scan skipped: active workflow_risk run exists: run_id=%s", active_run)
            return {"status": "skipped", "reason": "active_run_exists"}

        run = await request_agent_analysis(
            session,
            client,
            agent_type=RISK_AGENT_TYPE,
            trigger_source="scheduler",
        )
    logger.info("risk scan enqueued agent run: run_id=%s", run.id)
    return {"status": "enqueued", "run_id": str(run.id)}
