"""通过 `POST {BASE_URL}/chat/completions` 调用 OpenAI 兼容模型。

使用 Bearer Key 认证，可接入兼容 OpenAI Chat Completions 协议的网关或云服务。
`is_external=True` 表示数据会发送至外部服务，前端必须明确提示。
"""

import asyncio

import httpx

from app.infrastructure.models.errors import ModelTimeoutError, ModelUnavailableError
from app.infrastructure.models.provider import ModelProvider

_RETRY_BACKOFF_SECONDS = 0.5


class OpenAICompatibleProvider(ModelProvider):
    name = "openai_compatible"
    is_external = True  # 云端服务会接收业务数据，界面须提示数据外发。

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 60.0,
        max_retries: int = 2,
        max_tokens: int = 4096,
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
        self.max_tokens = max_tokens
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
        payload: dict = {
            "model": self.model,
            "messages": messages,
            # 显式给足生成额度：推理模型的 thinking 也占 token，服务端默认
            # 额度偏小会把结构化 JSON 输出截断（MiniMax M2.x 实测）
            "max_tokens": self.max_tokens,
        }
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
                    # 响应体不含 API Key，且日志不得记录请求头等敏感信息。
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
