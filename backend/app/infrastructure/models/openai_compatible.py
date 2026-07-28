"""OpenAI 兼容 Provider（可选，第 15 章）：POST {BASE_URL}/chat/completions。

- Bearer Key 认证；任何兼容 OpenAI Chat Completions 协议的网关/云服务均可接入；
- is_external=True：数据将发送至外部服务，前端须明确提示（16 节，T5.7 读取）；
- 与 OllamaProvider 同一 ModelProvider 接口，切换仅需改 LLM_PROVIDER 配置。
"""

import asyncio

import httpx

from app.infrastructure.models.errors import ModelTimeoutError, ModelUnavailableError
from app.infrastructure.models.provider import ModelProvider

_RETRY_BACKOFF_SECONDS = 0.5


class OpenAICompatibleProvider(ModelProvider):
    name = "openai_compatible"
    is_external = True  # 云端/外部服务：界面须提示数据外发（16 节）

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 60.0,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url:
            raise RuntimeError("OpenAI 兼容 Provider 需要配置 OPENAI_COMPATIBLE_BASE_URL")
        if not model:
            raise RuntimeError("OpenAI 兼容 Provider 需要配置 LLM_MODEL")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        # transport 仅供测试注入 MockTransport；生产为 None（httpx 默认网络传输）
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
        payload: dict = {"model": self.model, "messages": messages}
        if json_output:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}"}

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    base_url=self.base_url,
                    timeout=httpx.Timeout(self.timeout),
                    headers=headers,
                    transport=self._transport,
                ) as client:
                    resp = await client.post("/chat/completions", json=payload)
                if resp.status_code >= 400:
                    # 不记录响应全文以外的敏感信息；响应体不含 API Key
                    raise ModelUnavailableError(
                        f"OpenAI 兼容服务返回 HTTP {resp.status_code}: {resp.text[:500]}"
                    )
                data = resp.json()
                return str(data["choices"][0]["message"]["content"])
            except httpx.TimeoutException as exc:
                last_error = ModelTimeoutError(
                    f"OpenAI 兼容服务调用超时（{self.timeout}s，第 {attempt + 1} 次）"
                )
                last_error.__cause__ = exc
            except httpx.TransportError as exc:
                last_error = ModelUnavailableError(f"OpenAI 兼容服务不可达: {exc}")
                last_error.__cause__ = exc
            if attempt < self.max_retries:
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
        assert last_error is not None
        raise last_error
