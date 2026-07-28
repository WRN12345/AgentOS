"""Agent 分析触发接口的入参/出参 Schema（12.5 节，T5.4）。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class AgentAnalysisIn(BaseModel):
    """人工触发 Agent 分析入参（POST /work-items/{id}/agent-analysis）。

    agent_type 限定已注册能力（app.agents.graphs.base.AGENT_ROUTES，
    路由层校验）；prompt 为可选的自然语言输入（如需求原文）。
    """

    agent_type: str = Field(min_length=1, max_length=64)
    prompt: str = Field(default="", max_length=8000)


class ProjectAgentAnalysisIn(BaseModel):
    """项目级 Agent 分析入参（POST /agent-analysis，T5.5）。

    供项目级 Agent（workflow_risk / summary_agent 等无单一工作项的分析）
    使用；work_item_id 可空，给出时限定分析关联的工作项。
    """

    agent_type: str = Field(min_length=1, max_length=64)
    work_item_id: uuid.UUID | None = None
    prompt: str = Field(default="", max_length=8000)


class AgentRunOut(BaseModel):
    """agent_runs 运行信息（触发接口 202 响应 / 运行列表，T5.7）。"""

    id: uuid.UUID
    agent_type: str
    status: str
    model: str | None
    trigger_source: str
    work_item_id: uuid.UUID | None
    request_id: str | None
    created_at: datetime
    # 运行列表/详情补充字段（触发响应不填，默认 None）
    error: str | None = None
    duration_ms: int | None = None
    retry_count: int = 0
