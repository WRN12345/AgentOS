"""具体 Agent 能力共用的模型调用与输出构造工具。

模型超时或不可用错误不包装，由 worker 统一处理。build_output 会覆盖模型返回的
suggestion_type、prompt_version 和 fact_refs；非法 JSON 原样交给 validate_output 诊断。
"""

import json
import re
import uuid
from typing import Any

from app.infrastructure.models.provider import get_model_provider

# 部分推理模型会在 JSON 前附加 <think>，兼容服务也可能忽略 response_format
# 并返回 Markdown 围栏，因此统一在入口清理。
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
    """从图状态读取当前项目 UUID，供工具执行项目隔离。

    值来自 load_context 填充的 context.project.id。缺失时返回 None，工具层按无项目
    上下文处理并返回空集，保持 fail-closed。
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
    """将模型 JSON 转为建议 dict，并注入系统侧权威字段。

    suggestion_type、prompt_version 和 fact_refs 均由能力函数提供，不信任模型自报值。
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw  # 保留原始输出，交给 validate_output 生成 json_parse 诊断
    if not isinstance(payload, dict):
        return raw  # 保留原始输出，交给 validate_output 生成 schema_validate 诊断
    payload["suggestion_type"] = suggestion_type
    payload["prompt_version"] = prompt_version
    payload["fact_refs"] = fact_refs
    return payload
