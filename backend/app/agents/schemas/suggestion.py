"""Agent 建议的统一输出 Schema。

每条建议包含类型、内容和理由、业务事实引用、置信度、风险、模型、提示词版本和
运行 ID。模型和运行 ID 由系统填充，因此契约分为两层：

- AgentSuggestionOutput：能力函数或模型 JSON 必须满足的输出契约；
- AgentSuggestionEnvelope：系统补充 run_id 和 model 后的完整建议。

校验失败抛 SuggestionValidationError，诊断 JSON 包含错误详情、截断的原始输出和
run_id，由 worker 原样写入 agent_runs.error。
"""

import json
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

#: 截断诊断中的原始输出，避免超长模型响应撑大 agent_runs.error。
RAW_OUTPUT_MAX_CHARS = 500


class SuggestionContent(BaseModel):
    """建议内容和理由。

    extra="allow"：各能力可在 summary/rationale 之外附加自有结构化字段
    （如 echo 能力的回显块、分配建议的候选人列表），随 content 整体入库。
    """

    model_config = ConfigDict(extra="allow")

    summary: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class AgentSuggestionOutput(BaseModel):
    """模型输出部分：能力函数产出（或模型 JSON 解析后）必须满足的契约。"""

    suggestion_type: str = Field(min_length=1, max_length=64)
    content: SuggestionContent
    fact_refs: dict[str, list[str]] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)
    risks: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1, max_length=64)


class AgentSuggestionEnvelope(AgentSuggestionOutput):
    """系统填充 run_id 和 model 后的完整建议。"""

    run_id: uuid.UUID
    # 建议行不冗余存储模型名，以 agent_runs.model 为准。
    model: str | None = None


class PipelineAssignee(BaseModel):
    """pipeline 拆解项的推荐负责人/候选人（member_id 为真实成员 ID 字符串）。"""

    model_config = ConfigDict(extra="forbid")

    member_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class PipelineWorkItem(BaseModel):
    """pipeline 拆解工作项：title/description/验收标准/优先级/建议 DDL + 分配建议。

    user_specified 由系统按需求文本点名结果设置，Agent 不得更换用户指定的人选；
    notes 承载对指定人选的合理性提示（技能不匹配、
    负载过高），不阻止分配。
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    acceptance_criteria: str = Field(min_length=1)
    priority: str = Field(min_length=1)
    suggested_due_at: str | None = None
    recommended_assignee: PipelineAssignee | None = None
    candidates: list[PipelineAssignee] = Field(default_factory=list)
    user_specified: bool = False
    notes: str = ""


class PipelineSuggestionContent(SuggestionContent):
    """requirement_pipeline 的需求分析、拆解和分配 content 契约。

    extra="forbid"：该载荷形状是前端向导的依赖契约，多余字段一律拒绝。
    """

    model_config = ConfigDict(extra="forbid")

    goals: list[str]
    constraints: list[str]
    deliverables: list[str]
    acceptance_criteria: list[str]
    involved_aspects: list[str]
    work_item_breakdown: list[PipelineWorkItem] = Field(min_length=1)
    collaboration_points: list[str]
    unresolved_mentions: list[str]
    risks: list[str]
    # ok 表示已参考记忆，degraded 表示本次未参考记忆。
    memory_status: Literal["ok", "degraded"] = "ok"


class DevDocChecklistItem(BaseModel):
    """文档完整性检查项：目标/方案/接口/排期/风险逐项过。"""

    model_config = ConfigDict(extra="forbid")

    aspect: str = Field(min_length=1)
    verdict: Literal["pass", "fail", "uncertain"]
    note: str = ""


class DevDocReviewSuggestionContent(SuggestionContent):
    """dev_doc_review 的 content 契约：初审清单 + 对齐度 + 整体结论。

    extra="forbid"：该载荷形状是前端初审面板的依赖契约，多余字段一律拒绝。
    """

    model_config = ConfigDict(extra="forbid")

    checklist: list[DevDocChecklistItem] = Field(min_length=1)
    alignment: str = Field(min_length=1)
    verdict: Literal["sufficient", "needs_work"]
    risks: list[str]


#: 特定 suggestion_type 需在通用 Schema 之外追加严格 content 校验。
CONTENT_PAYLOAD_MODELS: dict[str, type[SuggestionContent]] = {
    "pipeline": PipelineSuggestionContent,
    "dev_doc_review": DevDocReviewSuggestionContent,
}


class SuggestionValidationError(ValueError):
    """结构校验失败，并携带可写入运行记录的诊断信息。

    str() 为 JSON 文本：{"run_id", "stage", "errors", "raw_output"(截断)}，
    worker 的通用失败处理会原样写入 agent_runs.error。
    """

    def __init__(
        self,
        *,
        run_id: str,
        stage: str,
        errors: list[Any],
        raw_output: Any,
    ) -> None:
        self.diagnostics: dict[str, Any] = {
            "run_id": str(run_id),
            "stage": stage,
            "errors": errors,
            "raw_output": str(raw_output)[:RAW_OUTPUT_MAX_CHARS],
        }
        super().__init__(json.dumps(self.diagnostics, ensure_ascii=False, default=str))


def parse_suggestion_output(raw: Any, *, run_id: str) -> AgentSuggestionOutput:
    """将能力产出校验为统一 Schema。

    接受两种形态：
    - dict（能力函数直接构造的结构化输出）；
    - str（模型返回的 JSON 文本，先解析再校验）。
    非法 JSON / 缺字段 / 类型错误一律抛 SuggestionValidationError。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SuggestionValidationError(
                run_id=run_id,
                stage="json_parse",
                errors=[f"模型输出不是合法 JSON: {exc}"],
                raw_output=raw,
            ) from exc
    try:
        output = AgentSuggestionOutput.model_validate(raw)
    except ValidationError as exc:
        raise SuggestionValidationError(
            run_id=run_id,
            stage="schema_validate",
            errors=exc.errors(),
            raw_output=raw,
        ) from exc
    payload_model = CONTENT_PAYLOAD_MODELS.get(output.suggestion_type)
    if payload_model is not None:
        try:
            payload_model.model_validate(output.content.model_dump(mode="json"))
        except ValidationError as exc:
            raise SuggestionValidationError(
                run_id=run_id,
                stage="schema_validate",
                errors=exc.errors(),
                raw_output=raw,
            ) from exc
    return output
