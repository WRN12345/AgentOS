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
from typing import Any

from app.infrastructure.models.provider import get_model_provider


async def call_model_json(*, system: str, user_prompt: str) -> str:
    """调用模型生成结构化 JSON 文本（提示词里已声明"只输出 JSON"）。"""
    provider = get_model_provider()
    return await provider.generate(user_prompt, system=system, json_output=True)


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
