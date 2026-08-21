"""EmbeddingProvider 适配层测试（M1.3/M1.4 验收，设计文档第 5 节、16.4、16.5）。

- M1.3：接口契约（fake provider）与配置默认值；
- M1.4：Ollama 实现用 httpx.MockTransport 验证请求格式与统一错误封装，
  不依赖真实 Ollama。
"""

import json

import httpx
import pytest

from app.core.config import settings
from app.infrastructure.models.embedding import (
    EmbeddingProvider,
    get_embedding_provider,
    reset_embedding_provider,
)
from app.infrastructure.models.errors import ModelTimeoutError, ModelUnavailableError
from app.infrastructure.models.ollama_embedding import OllamaEmbeddingProvider


class FakeEmbeddingProvider(EmbeddingProvider):
    name = "fake"
    model = "fake-embedding:v1"
    dimensions = 4

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(i)] * self.dimensions for i, _ in enumerate(texts)]


async def test_fake_provider_contract() -> None:
    """接口契约：返回与输入等长、每条维度等于 dimensions；空输入返回空。"""
    provider = FakeEmbeddingProvider()

    vectors = await provider.embed(["甲", "乙", "丙"])

    assert len(vectors) == 3
    assert all(len(v) == provider.dimensions for v in vectors)
    assert await provider.embed([]) == []


def test_embedding_config_defaults() -> None:
    """配置默认值：qwen3-embedding:0.6b / 1024 维（设计文档 15.1）。"""
    assert settings.embedding_model == "qwen3-embedding:0.6b"
    assert settings.embedding_dimensions == 1024


@pytest.fixture(autouse=True)
def _reset_provider():
    reset_embedding_provider()
    yield
    reset_embedding_provider()


def _make_provider(handler, **kwargs) -> OllamaEmbeddingProvider:
    return OllamaEmbeddingProvider(
        "http://host.docker.internal:11434",
        "qwen3-embedding:0.6b",
        4,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


async def test_ollama_embed_success() -> None:
    """批量 embed 成功：路径与请求体正确，返回浮点向量。"""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]})

    provider = _make_provider(handler)
    vectors = await provider.embed(["部署步骤", "发布流程"])

    assert vectors == [[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]
    req = captured[0]
    assert req.url.path == "/api/embed"
    body = json.loads(req.content)
    assert body == {"model": "qwen3-embedding:0.6b", "input": ["部署步骤", "发布流程"]}


async def test_ollama_embed_empty_input_no_http() -> None:
    """空输入直接返回空，不发 HTTP 请求。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"embeddings": []})

    provider = _make_provider(handler)
    assert await provider.embed([]) == []
    assert calls == 0


async def test_ollama_embed_dimension_mismatch_no_retry() -> None:
    """返回维度与配置不符 → ModelUnavailableError，且不重试（配置错误重试无意义）。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2]]})

    provider = _make_provider(handler, max_retries=2)
    with pytest.raises(ModelUnavailableError):
        await provider.embed(["测试"])
    assert calls == 1


async def test_ollama_embed_unavailable_wrapped() -> None:
    """Ollama 停机（连接失败）→ ModelUnavailableError，重试 max_retries 次，不漏 httpx 异常。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection refused", request=request)

    provider = _make_provider(handler, max_retries=2)
    with pytest.raises(ModelUnavailableError) as exc_info:
        await provider.embed(["ping"])
    assert not isinstance(exc_info.value, httpx.HTTPError)
    assert calls == 3


async def test_ollama_embed_timeout_wrapped() -> None:
    """读取超时 → ModelTimeoutError，不漏 httpx.TimeoutException。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    provider = _make_provider(handler, max_retries=0)
    with pytest.raises(ModelTimeoutError):
        await provider.embed(["ping"])


async def test_factory_returns_ollama_embedding() -> None:
    """工厂默认返回 OllamaEmbeddingProvider（单例）。"""
    provider = get_embedding_provider()
    assert isinstance(provider, OllamaEmbeddingProvider)
    assert isinstance(provider, EmbeddingProvider)
    assert provider.model == settings.embedding_model
    assert provider.dimensions == settings.embedding_dimensions
    assert get_embedding_provider() is provider
