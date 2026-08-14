"""具体 Agent 能力的公共助手（T5.4）。

- call_model_json：经 get_model_provider() 调模型并要求 JSON 输出
  （json_output=True）；模型超时/不可用错误（ModelTimeoutError /
  ModelUnavailableError）不做包装直接冒泡，由 worker 统一把 run 标记
  failed（17.3 节，指数退避重试在 T5.6 落地）。
- build_output：解析模型 JSON 并注入系统侧权威字段（suggestion_type /
  prompt_version / fact_refs），返回 dict 交 validate_output 走统一
  Schema 校验；模型输出不是合法 JSON 对象时原样透传字符串，由
  validate_output 产生 json_parse / schema_validate 诊断（17.3 节）。
"""

import json
import re
import uuid
from typing import Any

from app.infrastructure.models.provider import get_model_provider

# 推理模型（如 MiniMax M2.x，thinking 无法关闭）即使要求 JSON 输出，
# content 前部也会带 <think>...</think>；response_format 被部分兼容服务
# 忽略时还可能包 ```json 围栏。统一在入口处剥离。
_THINK_RE = re.compile(r"<think>.*?(</think>|$)", re.DOTALL)
_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL)


def strip_model_noise(raw: str) -> str:
    """剥离模型输出的 <think> 思考段与 Markdown 代码围栏，返回纯 JSON 文本。"""
    text = _THINK_RE.sub("", raw).strip()
    fence = _FENCE_RE.match(text)
    if fence:
        text = fence.group(1).strip()
    return text


def context_project_id(state: dict[str, Any]) -> uuid.UUID | None:
    """从图状态取当前项目 UUID（工具按项目过滤用，ticket 05）。

    取自 load_context 填充的 context.project.id；缺失返回 None
    （工具层按 project_id=None 视为无项目上下文，查询返回空集，fail-closed）。
    """
    project = (state.get("context") or {}).get("project")
    project_id = project.get("id") if isinstance(project, dict) else None
    return uuid.UUID(project_id) if project_id else None


async def call_model_json(*, system: str, user_prompt: str) -> str:
    """调用模型生成结构化 JSON 文本（提示词里已声明"只输出 JSON"）。"""
    provider = get_model_provider()
    raw = await provider.generate(user_prompt, system=system, json_output=True)
    return strip_model_noise(raw)


def build_output(
    raw: str,
    *,
    suggestion_type: str,
    prompt_version: str,
    fact_refs: dict[str, list[str]],
) -> Any:
    """模型 JSON → 建议 dict（注入系统侧字段后交 validate_output 校验）。

    suggestion_type / prompt_version / fact_refs 由能力函数声明（提示词由能力
    持有，事实引用来自真实查询数据），不信任模型自报值，直接覆盖。
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw  # 透传：validate_output 抛 json_parse 诊断
    if not isinstance(payload, dict):
        return raw  # 透传：validate_output 抛 schema_validate 诊断
    payload["suggestion_type"] = suggestion_type
    payload["prompt_version"] = prompt_version
    payload["fact_refs"] = fact_refs
    return payload
