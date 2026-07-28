"""模型适配层（第 15 章，T5.1）：业务/Agent 代码只依赖 ModelProvider 接口。

- 默认 OllamaProvider（OLLAMA_BASE_URL 指向宿主机 Ollama）；
- 可切换 OpenAICompatibleProvider（OPENAI_COMPATIBLE_BASE_URL + API Key）；
- 业务代码禁止直接实例化具体 Provider，统一经 get_model_provider() 工厂获取；
- is_external 标识当前 Provider 是否为外部（云端）服务，供 T5.7 前端提示
  "数据将发送至外部服务"（16 节）。
"""

from abc import ABC, abstractmethod

from app.core.config import settings


class ModelProvider(ABC):
    """模型调用最小接口：system + user 提示词 → 文本（可选结构化 JSON 输出）。"""

    #: Provider 名称（如 "ollama" / "openai_compatible"）
    name: str
    #: 实际调用的模型名
    model: str
    #: 是否为外部（云端）服务；True 时前端须提示数据外发（16 节）
    is_external: bool

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_output: bool = False,
    ) -> str:
        """生成一段文本。

        - prompt：user 消息正文（只放完成当前分析所需的最小上下文，16 节）；
        - system：可选 system 消息；
        - json_output：True 时要求模型输出合法 JSON（结构化输出场景）；
        - 超时抛 ModelTimeoutError，服务不可用/非 2xx 抛 ModelUnavailableError。
        """


_provider: ModelProvider | None = None


def get_model_provider() -> ModelProvider:
    """Provider 单例工厂（参照 storage 的 get_storage_provider 模式）。

    业务/Agent 代码一律经此函数获取模型能力；切换 Provider 只改
    LLM_PROVIDER 配置，代码零改动。
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
            )
        else:
            raise RuntimeError(f"不支持的模型 Provider: {settings.llm_provider}")
    return _provider


def reset_model_provider() -> None:
    """清空单例（测试切换 LLM_PROVIDER 配置后调用）。"""
    global _provider
    _provider = None
