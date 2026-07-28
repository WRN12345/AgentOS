"""Agent 建议查询与反馈接口的 Schema（12.5 节，T5.7）。

AgentSuggestionOut 在 agent_suggestions 行之上补充关联运行信息
（work_item_id / model 取自 agent_runs），便于建议中心直接渲染关联链接
与模型名而无需二次查询。
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentSuggestionOut(BaseModel):
    """建议列表/详情返回（含关联运行的 work_item_id 与 model）。"""

    id: uuid.UUID
    run_id: uuid.UUID
    suggestion_type: str
    content: dict[str, Any]
    confidence: float | None
    risks: str | None
    fact_refs: dict[str, Any] | None
    review_status: str
    reviewed_by: uuid.UUID | None
    reviewed_at: datetime | None
    prompt_version: str | None
    # 取自关联 agent_runs（项目级建议为 None，前端据此不渲染工作项链接）
    work_item_id: uuid.UUID | None
    model: str | None
    created_at: datetime


class AgentSuggestionFeedbackIn(BaseModel):
    """人工反馈入参（POST /agent-suggestions/{id}/feedback）。

    AgentSuggestion 表无 comment 字段（models.py），首版反馈只记录
    采纳/忽略结论与操作人/时间。
    """

    action: Literal["accepted", "ignored"]


class AgentConfigOut(BaseModel):
    """GET /config 返回的前端可用配置（T5.7：外部数据提示，16 节）。"""

    llm_provider: str
    llm_is_external: bool = Field(description="当前模型 Provider 是否为外部（云端）服务")
