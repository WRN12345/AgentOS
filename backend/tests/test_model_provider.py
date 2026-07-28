"""ModelProvider 适配层测试（T5.1 验收，15、17.3 节）。

全部用 httpx.MockTransport，不依赖真实 Ollama / 外部服务：
- Ollama 成功调用（/api/chat，system+user，format=json）；
- 切换 OpenAI 兼容配置时代码零改动（同一 ModelProvider 接口）；
- 服务不可用/超时 → 统一封装的 ModelUnavailableError / ModelTimeoutError，
  不向上层泄漏 httpx 异常；
- settings.llm_is_external 外部服务标识（供 T5.7）。
"""

import json

import httpx
import pytest

from app.core.config import settings
from app.infrastructure.models.errors import ModelTimeoutError, ModelUnavailableError
from app.infrastructure.models.ollama import OllamaProvider
from app.infrastructure.models.openai_compatible import OpenAICompatibleProvider
from app.infrastructure.models.provider import (
    ModelProvider,
    get_model_provider,
    reset_model_provider,
)


@pytest.fixture(autouse=True)
def _reset_provider():
    reset_model_provider()
    yield
    reset_model_provider()


def _ollama_ok_transport(captured: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": '{"ok": true}'}},
        )

    return httpx.MockTransport(handler)


def _openai_ok_transport(captured: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "你好"}}]},
        )

    return httpx.MockTransport(handler)


async def test_ollama_generate_success() -> None:
    """Ollama Provider 成功调用：路径、system/user 消息与 format=json 均正确。"""
    captured: list[httpx.Request] = []
    provider = OllamaProvider(
        "http://host.docker.internal:11434",
        "qwen2.5:7b",
        transport=_ollama_ok_transport(captured),
    )
    assert provider.is_external is False

    result = await provider.generate("总结一下", system="你是助手", json_output=True)

    assert result == '{"ok": true}'
    assert len(captured) == 1
    req = captured[0]
    assert req.url.path == "/api/chat"
    body = json.loads(req.content)
    assert body["model"] == "qwen2.5:7b"
    assert body["stream"] is False
    assert body["format"] == "json"
    assert body["messages"] == [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "总结一下"},
    ]


async def test_factory_switch_to_openai_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM_PROVIDER=openai_compatible 时工厂零代码改动切换到 OpenAI 兼容 Provider。"""
    monkeypatch.setattr(settings, "llm_provider", "openai_compatible")
    monkeypatch.setattr(settings, "llm_model", "gpt-4o-mini")
    monkeypatch.setattr(settings, "openai_compatible_base_url", "https://llm.example.com/v1")
    monkeypatch.setattr(settings, "openai_compatible_api_key", "sk-test")

    provider = get_model_provider()
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.is_external is True
    assert settings.llm_is_external is True

    # 同一生成接口可用（替换 transport 后调用）
    captured: list[httpx.Request] = []
    provider._transport = _openai_ok_transport(captured)
    result = await provider.generate("你好", json_output=True)
    assert result == "你好"
    req = captured[0]
    assert req.url.path == "/v1/chat/completions"
    assert req.headers["Authorization"] == "Bearer sk-test"
    body = json.loads(req.content)
    assert body["response_format"] == {"type": "json_object"}


async def test_factory_defaults_to_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认 LLM_PROVIDER=ollama；is_external 为 False。"""
    monkeypatch.setattr(settings, "llm_provider", "ollama")
    monkeypatch.setattr(settings, "llm_model", "qwen2.5:7b")
    provider = get_model_provider()
    assert isinstance(provider, OllamaProvider)
    assert isinstance(provider, ModelProvider)
    assert settings.llm_is_external is False


async def test_unavailable_raises_wrapped_error() -> None:
    """Ollama 停机（连接失败）→ ModelUnavailableError，重试 max_retries 次，不漏 httpx 异常。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection refused", request=request)

    provider = OllamaProvider(
        "http://host.docker.internal:11434",
        "qwen2.5:7b",
        max_retries=2,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ModelUnavailableError) as exc_info:
        await provider.generate("ping")
    assert not isinstance(exc_info.value, httpx.HTTPError)
    assert calls == 3  # 首次 + 2 次重试


async def test_timeout_raises_wrapped_error() -> None:
    """读取超时 → ModelTimeoutError，不漏 httpx.TimeoutException。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    provider = OllamaProvider(
        "http://host.docker.internal:11434",
        "qwen2.5:7b",
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ModelTimeoutError):
        await provider.generate("ping")


async def test_non_2xx_raises_unavailable_without_retry() -> None:
    """非 2xx 响应 → ModelUnavailableError，且不重试。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, text="internal error")

    provider = OpenAICompatibleProvider(
        "https://llm.example.com/v1",
        "sk-test",
        "gpt-4o-mini",
        max_retries=2,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ModelUnavailableError):
        await provider.generate("ping")
    assert calls == 1
