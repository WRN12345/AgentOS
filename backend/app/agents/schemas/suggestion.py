"""Agent 建议统一输出 Schema（10.2 节，T5.3）。

按 10.2 节要求，每条 Agent 建议至少包含：建议类型、建议内容和理由、
使用的业务事实引用、置信度、风险和限制、模型、提示词版本和运行 ID。
其中模型 / 运行 ID 由系统侧填充而非模型输出，故拆分为两层：

- AgentSuggestionOutput「模型输出部分」：能力函数（T5.4/T5.5 的六个 Agent）
  或解析模型 JSON 后必须满足的契约；prompt_version 由能力函数随提示词一起
  声明（提示词由能力持有，系统侧无从得知，故留在输出部分）。
- AgentSuggestionEnvelope「信封部分」：系统侧补充 run_id / model 后的完整
  建议。run_id 贯穿 agent_runs ↔ agent_suggestions；model 冗余记录在建
  议写入时的运行上下文（运行行上的 agent_runs.model 为准）。

校验失败抛 SuggestionValidationError：诊断信息（错误详情、原始输出截断、
run_id）以 JSON 文本形式承载，worker 原样落入 agent_runs.error（17.3 节）。
"""

import json
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

#: 诊断信息中原始输出的截断长度（避免 agent_runs.error 被超长模型输出撑爆）
RAW_OUTPUT_MAX_CHARS = 500


class SuggestionContent(BaseModel):
    """建议内容和理由（10.2 节）。

    extra="allow"：各能力可在 summary/rationale 之外附加自有结构化字段
    （如 echo 能力的回显块、分配建议的候选人列表），随 content 整体入库。
    """

    model_config = ConfigDict(extra="allow")

    # 建议内容（一句话结论）
    summary: str = Field(min_length=1)
    # 理由（为什么给出该建议）
    rationale: str = Field(min_length=1)


class AgentSuggestionOutput(BaseModel):
    """模型输出部分：能力函数产出（或模型 JSON 解析后）必须满足的契约。"""

    # 建议类型（如 requirement / assignment / planning / risk / review / summary / echo）
    suggestion_type: str = Field(min_length=1, max_length=64)
    content: SuggestionContent
    # 使用的业务事实引用（如 {"work_item_ids": [...], "member_ids": [...]}）
    fact_refs: dict[str, list[str]] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)
    # 风险和限制
    risks: str = Field(min_length=1)
    # 提示词版本（由能力函数随提示词声明，约定见 tools.py / base.py 注释）
    prompt_version: str = Field(min_length=1, max_length=64)


class AgentSuggestionEnvelope(AgentSuggestionOutput):
    """信封部分：系统侧填充 run_id / model 后的完整建议（10.2 节）。"""

    run_id: uuid.UUID
    # 运行时刻的模型名；建议行不冗余存储该字段（以 agent_runs.model 为准）
    model: str | None = None


# ---------- requirement_pipeline 专用载荷（设计文档 2026-07-30 §4.2） ----------


class PipelineAssignee(BaseModel):
    """pipeline 拆解项的推荐负责人/候选人（member_id 为真实成员 ID 字符串）。"""

    model_config = ConfigDict(extra="forbid")

    member_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class PipelineWorkItem(BaseModel):
    """pipeline 拆解工作项：title/description/验收标准/优先级/建议 DDL + 分配建议。

    user_specified 由系统侧按"需求文本点名解析结果"权威标记（用户指定的项
    Agent 不得更改人选）；notes 承载对指定人选的合理性提示（技能不匹配、
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
    """requirement_pipeline 的 content 契约（§4.2）：需求分析 + 拆解 + 分配。

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


# ---------- dev_doc_review 专用载荷（设计文档 2026-07-30 §4.4） ----------


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


#: suggestion_type → content 专用载荷模型。命中的类型在通用 Schema 之外
#: 追加一次严格载荷校验，失败同样抛 SuggestionValidationError。
CONTENT_PAYLOAD_MODELS: dict[str, type[SuggestionContent]] = {
    "pipeline": PipelineSuggestionContent,
    "dev_doc_review": DevDocReviewSuggestionContent,
}


class SuggestionValidationError(ValueError):
    """结构校验失败：携带诊断信息（17.3 节）。

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
    """把能力产出校验为统一 Schema（10.2、17.3 节）。

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
