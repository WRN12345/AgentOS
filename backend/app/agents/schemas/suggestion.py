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
from typing import Any

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
        return AgentSuggestionOutput.model_validate(raw)
    except ValidationError as exc:
        raise SuggestionValidationError(
            run_id=run_id,
            stage="schema_validate",
            errors=exc.errors(),
            raw_output=raw,
        ) from exc
