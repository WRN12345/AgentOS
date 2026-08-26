"""Ollama EmbeddingProvider（记忆模块默认实现）：POST {OLLAMA_BASE_URL}/api/embed。

- /api/embed 原生支持批量 input，一次调用返回全部向量；
- 统一错误封装与 OllamaProvider 一致：超时 → ModelTimeoutError，
  连接失败、非 2xx 或非法响应 → ModelUnavailableError，不泄漏底层异常；
- 返回维度与 settings.embedding_dimensions 不一致时视为服务异常
  （多半是 EMBEDDING_MODEL 与 EMBEDDING_DIMENSIONS 配错）。
"""

import asyncio

import httpx

from app.infrastructure.models.embedding import EmbeddingProvider
from app.infrastructure.models.errors import ModelTimeoutError, ModelUnavailableError

# 与 OllamaProvider 相同的线性退避基数（秒）
_RETRY_BACKOFF_SECONDS = 0.5


class OllamaEmbeddingProvider(EmbeddingProvider):
    name = "ollama"

    def __init__(
        self,
        base_url: str,
        model: str,
        dimensions: int,
        *,
        timeout: float = 60.0,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not model:
            raise RuntimeError("Ollama EmbeddingProvider 需要配置 EMBEDDING_MODEL")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dimensions = dimensions
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        # transport 仅供测试注入 MockTransport；生产为 None（httpx 默认网络传输）
        self._transport = transport

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = {"model": self.model, "input": texts}

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    base_url=self.base_url,
                    timeout=httpx.Timeout(self.timeout),
                    transport=self._transport,
                ) as client:
                    resp = await client.post("/api/embed", json=payload)
                if resp.status_code >= 400:
                    raise ModelUnavailableError(
                        f"Ollama embed 返回 HTTP {resp.status_code}: {resp.text[:500]}"
                    )
                try:
                    embeddings = resp.json()["embeddings"]
                    if len(embeddings) != len(texts) or any(
                        len(vector) != self.dimensions for vector in embeddings
                    ):
                        raise ModelUnavailableError(
                            f"Ollama embed 返回维度/数量与预期不符："
                            f"期望 {len(texts)}×{self.dimensions}，"
                            f"实际 {len(embeddings)}×"
                            f"{len(embeddings[0]) if embeddings else 0}"
                        )
                    return [list(map(float, vector)) for vector in embeddings]
                except (KeyError, TypeError, ValueError) as exc:
                    raise ModelUnavailableError("Ollama embed 返回非法响应") from exc
            except httpx.TimeoutException as exc:
                last_error = ModelTimeoutError(
                    f"Ollama embed 调用超时（{self.timeout}s，第 {attempt + 1} 次）"
                )
                last_error.__cause__ = exc
            except httpx.TransportError as exc:
                last_error = ModelUnavailableError(f"Ollama 不可达: {exc}")
                last_error.__cause__ = exc
            # 超时/连接失败按 max_retries 重试；非 2xx 与维度不符（主动抛出）不重试
            if attempt < self.max_retries:
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
        assert last_error is not None
        raise last_error
