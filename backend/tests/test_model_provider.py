"""验证 ModelProvider 适配层的请求契约、配置切换和错误封装。

测试使用 httpx.MockTransport，不依赖真实模型服务。连接失败和超时必须转换为
统一领域错误，不能向上层泄漏 httpx 异常。
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
    """Ollama Provider 应按约定发送路径、消息角色和 JSON 格式参数。"""
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
    """切换配置后工厂应返回 OpenAI 兼容 Provider。"""
    monkeypatch.setattr(settings, "llm_provider", "openai_compatible")
    monkeypatch.setattr(settings, "llm_model", "gpt-4o-mini")
    monkeypatch.setattr(settings, "openai_compatible_base_url", "https://llm.example.com/v1")
    monkeypatch.setattr(settings, "openai_compatible_api_key", "sk-test")

    provider = get_model_provider()
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.is_external is True
    assert settings.llm_is_external is True

    # 两种 Provider 共享同一生成接口契约。
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
    """Ollama 配置应创建内部模型 Provider。"""
    monkeypatch.setattr(settings, "llm_provider", "ollama")
    monkeypatch.setattr(settings, "llm_model", "qwen2.5:7b")
    provider = get_model_provider()
    assert isinstance(provider, OllamaProvider)
    assert isinstance(provider, ModelProvider)
    assert settings.llm_is_external is False


async def test_unavailable_raises_wrapped_error() -> None:
    """连接失败应按上限重试并封装为 ModelUnavailableError。"""
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
    assert calls == 3  # 最大重试数不包含首次请求。


async def test_timeout_raises_wrapped_error() -> None:
    """读取超时应封装为 ModelTimeoutError。"""

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
    """非成功响应应直接封装为 ModelUnavailableError，且不得重试。"""
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
