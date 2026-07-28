"""Ollama Provider（默认，第 15 章）：POST {OLLAMA_BASE_URL}/api/chat。

- 使用 /api/chat（支持 system+user 消息与 format=json 结构化输出）；
- 统一错误封装：超时 → ModelTimeoutError，连接失败/非 2xx → ModelUnavailableError；
- 不在顶层泄漏 httpx 异常；Ollama 停机等场景上层按 17.3 节策略处理。
"""

import asyncio

import httpx

from app.infrastructure.models.errors import ModelTimeoutError, ModelUnavailableError
from app.infrastructure.models.provider import ModelProvider

# 连接失败/响应非 2xx 时重试次数以外的退避基数（秒），简单线性退避即可
_RETRY_BACKOFF_SECONDS = 0.5


class OllamaProvider(ModelProvider):
    name = "ollama"
    is_external = False  # 本地/内网服务，不涉及数据外发（16 节）

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
            # 超时/连接失败按 max_retries 重试；非 2xx（ModelUnavailableError 主动抛出）不重试
            if attempt < self.max_retries:
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
        assert last_error is not None
        raise last_error
