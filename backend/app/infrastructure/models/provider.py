"""模型适配层，业务和 Agent 代码只依赖 `ModelProvider` 接口。

具体实现统一由 `get_model_provider()` 创建。`is_external` 标识 Provider 是否会向
外部服务发送数据，供前端显示数据外发提示。
"""

from abc import ABC, abstractmethod

from app.core.config import settings


class ModelProvider(ABC):
    """模型调用最小接口：system + user 提示词 → 文本（可选结构化 JSON 输出）。"""

    #: Provider 名称（如 "ollama" / "openai_compatible"）
    name: str
    #: 实际调用的模型名
    model: str
    #: 是否为外部服务；为 True 时前端须提示数据外发。
    is_external: bool

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_output: bool = False,
    ) -> str:
        """根据提示词生成文本。

        `prompt` 只应包含当前分析所需的最小上下文。`json_output=True` 时要求合法
        JSON。超时抛出 `ModelTimeoutError`，服务不可用或非 2xx 响应抛出
        `ModelUnavailableError`。
        """


_provider: ModelProvider | None = None


def get_model_provider() -> ModelProvider:
    """返回配置对应的模型 Provider 单例。

    业务和 Agent 代码应通过此函数获取模型能力，使 Provider 可仅通过配置切换。
    """
    global _provider
    if _provider is None:
        if settings.llm_provider == "ollama":
            from app.infrastructure.models.ollama import OllamaProvider

            _provider = OllamaProvider(
                base_url=settings.ollama_base_url,
                model=settings.llm_model,
                timeout=settings.llm_timeout_seconds,
                max_retries=settings.llm_max_retries,
            )
        elif settings.llm_provider == "openai_compatible":
            from app.infrastructure.models.openai_compatible import OpenAICompatibleProvider

            _provider = OpenAICompatibleProvider(
                base_url=settings.openai_compatible_base_url,
                api_key=settings.openai_compatible_api_key,
                model=settings.llm_model,
                timeout=settings.llm_timeout_seconds,
                max_retries=settings.llm_max_retries,
                max_tokens=settings.llm_max_tokens,
            )
        else:
            raise RuntimeError(f"不支持的模型 Provider: {settings.llm_provider}")
    return _provider


def reset_model_provider() -> None:
    """清空单例（测试切换 LLM_PROVIDER 配置后调用）。"""
    global _provider
    _provider = None
