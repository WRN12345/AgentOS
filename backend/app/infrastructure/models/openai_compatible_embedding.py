"""OpenAI 兼容 EmbeddingProvider（EMBEDDING_PROVIDER=openai_compatible）。

用于智谱 bigmodel（embedding-3）等 OpenAI 兼容的云端 embedding 服务：
POST {EMBEDDING_BASE_URL}/embeddings，Bearer Key 鉴权，OpenAI 响应格式
（data[].embedding/index），请求携带 dimensions 保证维度与
EMBEDDING_DIMENSIONS 一致（智谱 embedding-3 支持自定义维度）。

- 数据外发提示（16 节）：项目文档/档案/历史内容将发送至该第三方服务，
  部署方需自行评估；降级语义与 Ollama 实现一致（16.5，上层据此转无记忆模式）；
- 统一错误封装：超时 → ModelTimeoutError，连接失败/非 2xx → ModelUnavailableError；
- 返回维度与 settings.embedding_dimensions 不一致时视为服务异常
  （多半是 EMBEDDING_DIMENSIONS 配错）。
"""

import asyncio

import httpx

from app.infrastructure.models.embedding import EmbeddingProvider
from app.infrastructure.models.errors import ModelTimeoutError, ModelUnavailableError

# 与 Ollama 实现相同的线性退避基数（秒）
_RETRY_BACKOFF_SECONDS = 0.5


class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    name = "openai_compatible"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int,
        *,
        timeout: float = 60.0,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise RuntimeError(
                "OpenAI 兼容 embedding 服务需要配置 EMBEDDING_API_KEY"
            )
        if not model:
            raise RuntimeError("需要配置 EMBEDDING_MODEL")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dimensions = dimensions
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        # transport 仅供测试注入 MockTransport；生产为 None（httpx 默认网络传输）
        self._transport = transport

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = {"model": self.model, "input": texts, "dimensions": self.dimensions}
        headers = {"Authorization": f"Bearer {self.api_key}"}

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    base_url=self.base_url,
                    timeout=httpx.Timeout(self.timeout),
                    transport=self._transport,
                ) as client:
                    resp = await client.post("/embeddings", json=payload, headers=headers)
                if resp.status_code >= 400:
                    raise ModelUnavailableError(
                        f"embedding 服务返回 HTTP {resp.status_code}: {resp.text[:500]}"
                    )
                data = resp.json()["data"]
                # OpenAI 格式：按 index 排序保证与输入顺序一致
                embeddings = [
                    item["embedding"] for item in sorted(data, key=lambda d: d["index"])
                ]
                if len(embeddings) != len(texts) or any(
                    len(v) != self.dimensions for v in embeddings
                ):
                    raise ModelUnavailableError(
                        f"embedding 服务返回维度/数量与预期不符："
                        f"期望 {len(texts)}×{self.dimensions}，"
                        f"实际 {len(embeddings)}×{len(embeddings[0]) if embeddings else 0}"
                    )
                return [list(map(float, v)) for v in embeddings]
            except httpx.TimeoutException as exc:
                last_error = ModelTimeoutError(
                    f"embedding 调用超时（{self.timeout}s，第 {attempt + 1} 次）"
                )
                last_error.__cause__ = exc
            except httpx.TransportError as exc:
                last_error = ModelUnavailableError(f"embedding 服务不可达: {exc}")
                last_error.__cause__ = exc
            # 超时/连接失败按 max_retries 重试；非 2xx 与维度不符（主动抛出）不重试
            if attempt < self.max_retries:
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
        assert last_error is not None
        raise last_error
