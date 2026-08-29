"""Workflow Risk Agent 周期风险扫描。

由 scheduler 周期 enqueue `agent.risk_scan`、worker 消费执行：
按项目遍历，每项目检查无 pending/running 的同类型运行后，调用
request_agent_analysis（trigger_source="scheduler"）投递一次项目级
workflow_risk 分析；真正的图运行仍走 `agent.run` 队列，由 worker 统一承载。

去重以项目及其 `pending` 或 `running` 的 `workflow_risk` 运行为范围，使不同项目
互不影响，同时避免同一项目内重复堆积。
"""

from datetime import UTC, datetime, timedelta

import redis.asyncio as redis
from sqlalchemy import func, select, update

from app.agents.models import AgentRun
from app.agents.service import request_agent_analysis
from app.agents.specialists.risk import AGENT_TYPE as RISK_AGENT_TYPE
from app.core.logging import setup_logging
from app.domains.project.models import Project
from app.infrastructure.database.engine import async_session_factory

logger = setup_logging("worker")

#: scheduler 投递、worker 消费的任务类型
RISK_SCAN_TASK_TYPE = "agent.risk_scan"
RISK_SCAN_PENDING_LEASE = timedelta(minutes=30)


def _project_lock_key(project_id) -> int:  # noqa: ANN001
    """将项目 UUID 稳定映射为 PostgreSQL advisory lock 的正 63 位 key。"""
    return project_id.int & 0x7FFF_FFFF_FFFF_FFFF


async def run_risk_scan(client: redis.Redis) -> dict[str, object]:
    """执行一轮风险扫描触发：按项目去重后投递 workflow_risk 分析，返回结果标记。"""
    async with async_session_factory() as session:
        project_ids = list((await session.execute(select(Project.id))).scalars().all())

    result: dict[str, object] = {"status": "done", "enqueued": [], "skipped": []}
    for project_id in project_ids:
        async with async_session_factory() as session:
            locked = (
                await session.execute(
                    select(func.pg_try_advisory_xact_lock(_project_lock_key(project_id)))
                )
            ).scalar_one()
            if not locked:
                result["skipped"].append(str(project_id))
                continue

            stale_before = datetime.now(UTC) - RISK_SCAN_PENDING_LEASE
            await session.execute(
                update(AgentRun)
                .where(
                    AgentRun.agent_type == RISK_AGENT_TYPE,
                    AgentRun.project_id == project_id,
                    AgentRun.status == "pending",
                    AgentRun.updated_at < stale_before,
                )
                .values(
                    status="failed",
                    error="Risk scan pending lease expired before worker execution",
                )
            )
            await session.flush()
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
