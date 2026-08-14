"""Workflow Risk Agent 周期风险扫描（4.2 节逾期风险扫描，T5.5）。

由 scheduler 周期 enqueue `agent.risk_scan`、worker 消费执行：
按项目遍历，每项目检查无 pending/running 的同类型运行后，调用
request_agent_analysis（trigger_source="scheduler"）投递一次项目级
workflow_risk 分析；真正的图运行仍走 `agent.run` 队列，由 worker 统一承载。

去重（ticket 05 项目维度化）：以「项目 × 存在 pending/running 的
workflow_risk run」为去重键——A 项目有活跃 run 不再跳过 B 项目，
避免周期触发跨项目互相 skip；同一项目内仍避免重复堆积。
"""

import redis.asyncio as redis
from sqlalchemy import select

from app.agents.models import AgentRun
from app.agents.service import request_agent_analysis
from app.agents.specialists.risk import AGENT_TYPE as RISK_AGENT_TYPE
from app.core.logging import setup_logging
from app.domains.project.models import Project
from app.infrastructure.database.engine import async_session_factory

logger = setup_logging("worker")

#: scheduler 投递、worker 消费的任务类型
RISK_SCAN_TASK_TYPE = "agent.risk_scan"


async def run_risk_scan(client: redis.Redis) -> dict[str, object]:
    """执行一轮风险扫描触发：按项目去重后投递 workflow_risk 分析，返回结果标记。"""
    async with async_session_factory() as session:
        project_ids = list((await session.execute(select(Project.id))).scalars().all())

    result: dict[str, object] = {"status": "done", "enqueued": [], "skipped": []}
    for project_id in project_ids:
        async with async_session_factory() as session:
            active_run = (
                await session.execute(
                    select(AgentRun.id)
                    .where(
                        AgentRun.agent_type == RISK_AGENT_TYPE,
                        AgentRun.status.in_(("pending", "running")),
                        AgentRun.project_id == project_id,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if active_run is not None:
                logger.info(
                    "risk scan skipped: active workflow_risk run exists: project_id=%s run_id=%s",
                    project_id,
                    active_run,
                )
                result["skipped"].append(str(project_id))
                continue
            run = await request_agent_analysis(
                session,
                client,
                agent_type=RISK_AGENT_TYPE,
                project_id=project_id,
                trigger_source="scheduler",
            )
        result["enqueued"].append({"project_id": str(project_id), "run_id": str(run.id)})
        logger.info(
            "risk scan enqueued agent run: project_id=%s run_id=%s", project_id, run.id
        )
    return result
