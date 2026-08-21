"""Embedding 适配层（记忆模块，设计文档第 5 节）：业务代码只依赖 EmbeddingProvider 接口。

- 与 ModelProvider 同模式：默认 Ollama 实现（复用 OLLAMA_BASE_URL），
  统一经 get_embedding_provider() 工厂获取，业务代码禁止直接实例化；
- 错误封装复用 ModelTimeoutError / ModelUnavailableError，
  上层（Agent 上下文装配，16.5）据此判断降级为"无记忆模式"；
- dimensions 暴露向量维度，供 memory_chunks 建表（M1.6）与检索使用；
  更换模型时维度可能变化，须配合模型版本字段全量重建（16.4）。
"""

from abc import ABC, abstractmethod

from app.core.config import settings


class EmbeddingProvider(ABC):
    """文本向量化的最小接口：批量文本 → 等长向量列表。"""

    #: Provider 名称（如 "ollama"）
    name: str
    #: 实际调用的模型名（如 "qwen3-embedding:0.6b"），写入 memory_chunks.model_version（16.4）
    model: str
    #: 向量维度（qwen3-embedding:0.6b 为 1024）
    dimensions: int

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """把一批文本转成向量，返回与输入等长、每条维度为 dimensions 的列表。

        - 空列表输入返回空列表；
        - 超时抛 ModelTimeoutError，服务不可用/非 2xx 抛 ModelUnavailableError；
        - 不泄漏 httpx 等底层异常类型。
        """


_provider: EmbeddingProvider | None = None


def get_embedding_provider() -> EmbeddingProvider:
    """Provider 单例工厂（同 get_model_provider 模式）。

    业务/worker 代码一律经此函数获取 embedding 能力；当前仅 Ollama 实现，
    复用 OLLAMA_BASE_URL 与 LLM_TIMEOUT_SECONDS / LLM_MAX_RETRIES 配置。
    """
    global _provider
    if _provider is None:
        from app.infrastructure.models.ollama_embedding import OllamaEmbeddingProvider

        _provider = OllamaEmbeddingProvider(
            base_url=settings.ollama_base_url,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
    return _provider


def reset_embedding_provider() -> None:
    """清空单例（测试切换配置后调用）。"""
    global _provider
    _provider = None
