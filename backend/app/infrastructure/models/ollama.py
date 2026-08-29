"""通过 `POST {OLLAMA_BASE_URL}/api/chat` 调用 Ollama。

接口支持 system、user 消息和 `format=json` 结构化输出。超时转换为
`ModelTimeoutError`，连接失败或非 2xx 响应转换为 `ModelUnavailableError`，
不向上泄漏 httpx 异常。
"""

import asyncio

import httpx

from app.infrastructure.models.errors import ModelTimeoutError, ModelUnavailableError
from app.infrastructure.models.provider import ModelProvider

# 连接失败时使用线性退避，基数单位为秒。
_RETRY_BACKOFF_SECONDS = 0.5


class OllamaProvider(ModelProvider):
    name = "ollama"
    is_external = False  # 本地或内网服务不向第三方发送数据。

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout: float = 60.0,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not model:
            raise RuntimeError("Ollama Provider 需要配置 LLM_MODEL")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        # `transport` 仅供测试注入 MockTransport；生产环境使用 httpx 默认传输。
        self._transport = transport

    async def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_output: bool = False,
    ) -> str:
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        payload: dict = {"model": self.model, "messages": messages, "stream": False}
        if json_output:
            payload["format"] = "json"

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    base_url=self.base_url,
                    timeout=httpx.Timeout(self.timeout),
                    transport=self._transport,
                ) as client:
                    resp = await client.post("/api/chat", json=payload)
                if resp.status_code >= 400:
                    raise ModelUnavailableError(
                        f"Ollama 返回 HTTP {resp.status_code}: {resp.text[:500]}"
                    )
                data = resp.json()
                return str(data["message"]["content"])
            except httpx.TimeoutException as exc:
                last_error = ModelTimeoutError(
                    f"Ollama 调用超时（{self.timeout}s，第 {attempt + 1} 次）"
                )
                last_error.__cause__ = exc
            except httpx.TransportError as exc:
                last_error = ModelUnavailableError(f"Ollama 不可达: {exc}")
                last_error.__cause__ = exc
            # 仅超时和连接失败会进入此重试路径；非 2xx 响应直接向上抛出。
            if attempt < self.max_retries:
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
        assert last_error is not None
        raise last_error
