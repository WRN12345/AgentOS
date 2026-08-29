"""Agent 分析触发接口的入参与出参 Schema。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class AgentAnalysisIn(BaseModel):
    """POST /work-items/{id}/agent-analysis 的入参。

    agent_type 限定已注册能力（app.agents.graphs.base.AGENT_ROUTES，
    路由层校验）；prompt 为可选的自然语言输入（如需求原文）。
    """

    agent_type: str = Field(min_length=1, max_length=64)
    prompt: str = Field(default="", max_length=8000)


class ProjectAgentAnalysisIn(BaseModel):
    """POST /agent-analysis 的项目级分析入参。

    供项目级 Agent（workflow_risk / summary_agent 等无单一工作项的分析）
    使用；work_item_id 可空，给出时限定分析关联的工作项。
    """

    agent_type: str = Field(min_length=1, max_length=64)
    work_item_id: uuid.UUID | None = None
    prompt: str = Field(default="", max_length=8000)


class AgentRunOut(BaseModel):
    """agent_runs 运行信息，用于触发接口 202 响应和运行列表。"""

    id: uuid.UUID
    agent_type: str
    status: str
    model: str | None
    trigger_source: str
    work_item_id: uuid.UUID | None
    request_id: str | None
    created_at: datetime
    # 列表和详情补充以下诊断字段，触发响应保留默认值。
    error: str | None = None
    duration_ms: int | None = None
    retry_count: int = 0
