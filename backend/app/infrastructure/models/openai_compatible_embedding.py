"""OpenAI 兼容 EmbeddingProvider（EMBEDDING_PROVIDER=openai_compatible）。

用于智谱 bigmodel 等 OpenAI 兼容云端 embedding 服务，通过 Bearer Key 鉴权并按
OpenAI 格式读取 `data[].embedding/index`。请求维度必须与 `memory_chunks` 的
PostgreSQL vector 列一致。

项目文档、档案和历史内容会发送给第三方，部署方必须评估数据外发风险。超时转换为
`ModelTimeoutError`，连接失败、非 2xx 或非法响应转换为 `ModelUnavailableError`。
返回维度不一致视为服务或配置异常。
"""

import asyncio

import httpx

from app.infrastructure.models.embedding import EmbeddingProvider
from app.infrastructure.models.errors import ModelTimeoutError, ModelUnavailableError

# 与 Ollama 实现保持一致的线性退避基数，单位为秒。
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
        # `transport` 仅供测试注入 MockTransport；生产环境使用 httpx 默认传输。
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
                try:
                    data = resp.json()["data"]
                    # 按 OpenAI 响应中的 `index` 排序，保持与输入顺序一致。
                    embeddings = [
                        item["embedding"]
                        for item in sorted(data, key=lambda item: item["index"])
                    ]
                    if len(embeddings) != len(texts) or any(
                        len(vector) != self.dimensions for vector in embeddings
                    ):
                        raise ModelUnavailableError(
                            f"embedding 服务返回维度/数量与预期不符："
                            f"期望 {len(texts)}×{self.dimensions}，"
                            f"实际 {len(embeddings)}×"
                            f"{len(embeddings[0]) if embeddings else 0}"
                        )
                    return [list(map(float, vector)) for vector in embeddings]
                except (KeyError, TypeError, ValueError) as exc:
                    raise ModelUnavailableError("embedding 服务返回非法响应") from exc
            except httpx.TimeoutException as exc:
                last_error = ModelTimeoutError(
                    f"embedding 调用超时（{self.timeout}s，第 {attempt + 1} 次）"
                )
                last_error.__cause__ = exc
            except httpx.TransportError as exc:
                last_error = ModelUnavailableError(f"embedding 服务不可达: {exc}")
                last_error.__cause__ = exc
            # 仅超时和连接失败会进入此重试路径；响应或维度错误直接向上抛出。
            if attempt < self.max_retries:
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
        assert last_error is not None
        raise last_error
