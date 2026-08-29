"""Embedding 适配层，业务代码只依赖 `EmbeddingProvider` 接口。

具体实现统一由 `get_embedding_provider()` 创建。错误转换为 `ModelTimeoutError` 或
`ModelUnavailableError`，供上层降级为无记忆模式。更换模型后必须按模型版本全量
重建索引；若向量维度变化，还需先迁移 `memory_chunks` 的 vector 列。
"""

from abc import ABC, abstractmethod

from app.core.config import settings


class EmbeddingProvider(ABC):
    """文本向量化的最小接口：批量文本 → 等长向量列表。"""

    #: Provider 名称（如 "ollama"）
    name: str
    #: 实际调用的模型名，写入 `memory_chunks.model_version`。
    model: str
    #: 向量维度（qwen3-embedding:0.6b 为 1024）
    dimensions: int

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """把一批文本转换为向量。

        返回列表必须与输入等长，每条向量维度为 `dimensions`。空输入返回空列表；
        超时抛出 `ModelTimeoutError`，服务不可用或响应异常抛出
        `ModelUnavailableError`，不泄漏 httpx 等底层异常。
        """


_provider: EmbeddingProvider | None = None


def get_embedding_provider() -> EmbeddingProvider:
    """返回配置对应的 Embedding Provider 单例。

    `ollama` 使用本地 Ollama；`openai_compatible` 使用 OpenAI 兼容云服务，并会将
    项目文档发送给第三方。
    """
    global _provider
    if _provider is None:
        if settings.embedding_provider == "openai_compatible":
            from app.infrastructure.models.openai_compatible_embedding import (
                OpenAICompatibleEmbeddingProvider,
            )

            _provider = OpenAICompatibleEmbeddingProvider(
                base_url=settings.embedding_base_url,
                api_key=settings.embedding_api_key,
                model=settings.embedding_model,
                dimensions=settings.embedding_dimensions,
                timeout=settings.llm_timeout_seconds,
                max_retries=settings.llm_max_retries,
            )
        elif settings.embedding_provider == "ollama":
            from app.infrastructure.models.ollama_embedding import OllamaEmbeddingProvider

            _provider = OllamaEmbeddingProvider(
                base_url=settings.ollama_base_url,
                model=settings.embedding_model,
                dimensions=settings.embedding_dimensions,
                timeout=settings.llm_timeout_seconds,
                max_retries=settings.llm_max_retries,
            )
        else:
            raise RuntimeError(f"不支持的 embedding Provider: {settings.embedding_provider}")
    return _provider


def reset_embedding_provider() -> None:
    """清空单例（测试切换配置后调用）。"""
    global _provider
    _provider = None
